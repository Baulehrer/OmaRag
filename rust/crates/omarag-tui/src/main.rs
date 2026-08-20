use anyhow::{Context, Result};
use clap::Parser;
use crossterm::{
    event::{
        self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        Event,
    },
    execute,
    style::force_color_output,
    terminal::SetTitle,
};
use futures_util::StreamExt;
use image::{ImageReader, Pixel, Rgba};
use notify_debouncer_mini::{DebounceEventResult, new_debouncer, notify::RecursiveMode};
use omarag_app::{
    Action, AppState, DocumentInsight, HardwareProfile, ImportPreflight, InputMode,
    ModelCatalogResponse, ModelCategory, ModelSource, Notification, NotificationLevel, Overlay,
    PendingBookReview, UiPreferences, UndoAction, update,
};
use omarag_client::{HttpOmaRagClient, OmaRagClient};
use omarag_domain::{
    BackupSummary, CommitImportRequest, ConfigDocument, DocumentSummary, DomainEvent,
    EventSubscription, HardwareProfileResponse, JobId, JobSnapshot, ModelProfilePreflight,
    OmaRagError, PerformanceProfile, PreflightImportRequest, QualityReport, RetrievalExplanation,
    RunId, RunRequest, SourceDefinition, VisualEvidenceResponse, WorkspaceId, WorkspaceManifest,
    WorkspaceSummary,
};
use omarag_tui::{
    ChatImagePreview, LoadedModel, MediaImagePreview, ModelRoleStatus, RuntimeMetrics, Theme,
    VisualInspectorState, fallback_hardware_profile,
    input::{
        JobCommand, UiCommand, expand_import_paths, fuzzy_score, handle_event_with_visuals,
        refresh_file_browser,
    },
    performance_profile, related_page_refs, render_with_runtime,
};
use ratatui_image::picker::Picker;
use serde::Deserialize;
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{Cursor, Read, Write, stdout},
    process::{Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use sysinfo::System;
use tokio::sync::mpsc;
use tokio::time::Instant as TokioInstant;
use tokio_util::sync::CancellationToken;
use url::Url;
use uuid::Uuid;

#[derive(Debug, Parser)]
#[command(version, about = "OmaRag · answers from your own books")]
struct Args {
    #[arg(long, env = "OMARAG_URL", default_value = "http://127.0.0.1:8765")]
    url: Url,
    #[arg(long, env = "OMARAG_TOKEN")]
    token: Option<String>,
}

enum BackendMessage {
    WorkspaceOpened(Result<WorkspaceId, String>),
    WorkspaceCreated(Result<WorkspaceManifest, String>),
    LibraryDeleted {
        id: WorkspaceId,
        physical: bool,
        result: Result<(), String>,
    },
    ExternalOpened(Result<(), String>),
    ClipboardCopied {
        selection: bool,
        characters: usize,
        result: Result<(), String>,
    },
    ImportAnalyzed(ImportPreflight),
    DocumentDeleted(Result<omarag_domain::DocumentSummary, String>),
    DocumentRestored(Result<omarag_domain::DocumentSummary, String>),
    PreviewLoaded {
        key: (String, u32),
        result: Result<ChatImagePreview, String>,
    },
    MediaPreviewLoaded {
        key: String,
        result: Result<MediaImagePreview, String>,
    },
    VisualEvidenceLoaded {
        run_id: RunId,
        response: Option<VisualEvidenceResponse>,
    },
    HardwareProfileLoaded(Option<HardwareProfileResponse>),
    HardwareRecommendationLoaded {
        profile: PerformanceProfile,
        response: Option<HardwareProfileResponse>,
    },
    RunStarted(Result<RunId, String>),
    RunCancelled(Result<(), String>),
    SearchCompleted(Result<RetrievalExplanation, String>),
    ImportAccepted(Result<JobId, String>),
    JobUpdated(Result<(), String>),
    JobsLoaded(Result<Vec<JobSnapshot>, String>),
    WorkspaceFeaturesLoaded(Result<Box<WorkspaceFeatures>, String>),
    BackupCreated(Result<BackupSummary, String>),
    SourceCreated(Result<SourceDefinition, String>),
    ConfigSaved(Result<ConfigDocument, String>),
    AutomaticStackPreflight {
        workspace: WorkspaceId,
        result: Result<ModelProfilePreflight, String>,
    },
    AutomaticStackApplied(Result<ConfigDocument, String>),
    ModelCatalogLoaded(Result<ModelCatalogResponse, String>),
    ModelTransfer(ModelTransfer),
    ModelOperationFinished {
        model: String,
        operation: ModelOperation,
        result: Result<(), String>,
    },
    ModelPackageFinished {
        name: String,
        installed_models: Vec<String>,
        activation_status: String,
        result: Result<Option<ConfigDocument>, String>,
    },
    WarmupFinished,
    FilesystemChanged,
}

#[derive(Debug)]
struct PreviewScope {
    token: CancellationToken,
    keys: Vec<(String, u32)>,
}

#[derive(Debug)]
struct MediaPreviewScope {
    token: CancellationToken,
    keys: Vec<String>,
}

#[derive(Debug, Clone)]
struct CitationPreviewTarget {
    citation_index: usize,
    page_index: usize,
    path: String,
    page: u32,
    remote_preview: bool,
    title: String,
    primary_anchors: Vec<omarag_domain::CitationAnchor>,
    context_anchors: Vec<omarag_domain::CitationAnchor>,
}

impl Default for PreviewScope {
    fn default() -> Self {
        Self {
            token: CancellationToken::new(),
            keys: Vec::new(),
        }
    }
}

impl Default for MediaPreviewScope {
    fn default() -> Self {
        Self {
            token: CancellationToken::new(),
            keys: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Copy)]
enum ModelOperation {
    Download,
    Load,
    Unload,
    Delete,
}

#[derive(Debug)]
struct ModelTransfer {
    model: String,
    status: String,
    completed: u64,
    total: u64,
}

#[derive(Debug)]
struct WorkspaceFeatures {
    documents: Vec<DocumentSummary>,
    sources: Vec<SourceDefinition>,
    quality: QualityReport,
    backups: Vec<BackupSummary>,
    config: ConfigDocument,
    details: BTreeMap<String, DocumentInsight>,
}

#[derive(Debug, Clone)]
struct ModelApi {
    base_url: Url,
    token: Option<String>,
    client: reqwest::Client,
}

impl ModelApi {
    fn new(base_url: Url, token: Option<String>) -> Result<Self> {
        Ok(Self {
            base_url,
            token,
            client: reqwest::Client::builder()
                .user_agent(concat!("omarag-tui/", env!("CARGO_PKG_VERSION")))
                .build()?,
        })
    }

    fn request(&self, method: reqwest::Method, path: &str) -> Result<reqwest::RequestBuilder> {
        let url = self.base_url.join(path.trim_start_matches('/'))?;
        let mut request = self.client.request(method, url);
        if let Some(token) = &self.token {
            request = request.bearer_auth(token);
        }
        Ok(request)
    }
}

async fn model_operation_result(
    result: Result<reqwest::Response, reqwest::Error>,
) -> Result<(), String> {
    let response = result.map_err(|error| error.to_string())?;
    if response.status().is_success() {
        return Ok(());
    }
    let status = response.status();
    let body = response.text().await.unwrap_or_default();
    let message = serde_json::from_str::<serde_json::Value>(&body)
        .ok()
        .and_then(|payload| {
            payload
                .pointer("/error/message")
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)
        })
        .filter(|message| !message.is_empty())
        .unwrap_or_else(|| format!("Model operation failed with HTTP {status}"));
    Err(message)
}

#[derive(Debug, Deserialize)]
struct OllamaProcesses {
    #[serde(default)]
    models: Vec<OllamaModel>,
    #[serde(default)]
    roles: Vec<RuntimeRole>,
}

#[derive(Debug, Deserialize)]
struct RuntimeRole {
    role: String,
    model: Option<String>,
    residency: String,
    #[serde(default)]
    shared_with: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct OllamaModel {
    name: String,
    #[serde(default)]
    size: u64,
    #[serde(default)]
    size_vram: u64,
    #[serde(default)]
    context_length: u64,
    #[serde(default)]
    parameter_size: String,
    #[serde(default)]
    quantization_level: String,
}

#[derive(Debug, Deserialize)]
struct PullProgress {
    #[serde(default)]
    status: String,
    #[serde(default)]
    completed: u64,
    #[serde(default)]
    total: u64,
    #[serde(default)]
    error: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ModelDefaultsPreflightResponse {
    #[serde(default)]
    requires_reindex: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    // OmaRag is an explicitly themed full-screen application. Some desktop
    // sessions export NO_COLOR globally; override that preference for this
    // process so theme selection and keyboard focus remain visible.
    force_color_output(true);
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .with_writer(std::io::stderr)
        .init();
    let args = Args::parse();
    Theme::refresh_omarchy();
    let model_api = ModelApi::new(args.url.clone(), args.token.clone())?;
    let client = Arc::new(HttpOmaRagClient::new(args.url, args.token)?);
    // The registry must be known before preferences are applied, so a stored
    // index can be clamped and a stored name resolved.
    let mut state = AppState {
        theme_count: Theme::count(),
        ..AppState::default()
    };
    let preferences_path = omarag_config_dir().join("ui-state.json");
    if let Ok(preferences) = load_preferences(&preferences_path) {
        state.apply_preferences(preferences);
    }
    // A name survives the theme list changing; an index does not.
    if let Some(index) = Theme::index_of(&state.theme_name) {
        state.theme_index = index;
        state.theme_cursor = index;
    }
    state.theme_name = Theme::at(state.theme_index).name.to_owned();
    for problem in Theme::problems() {
        notify_error(&mut state, format!("Theme not loaded — {problem}"));
    }
    bootstrap(client.as_ref(), &mut state).await;

    let mut terminal = ratatui::try_init().context("Terminal initialization failed")?;
    let image_picker = Picker::from_query_stdio().unwrap_or_else(|_| Picker::halfblocks());
    execute!(
        stdout(),
        SetTitle("OmaRag"),
        EnableBracketedPaste,
        EnableMouseCapture
    )
    .context("Could not enable terminal input modes")?;
    let running = Arc::new(AtomicBool::new(true));
    let (terminal_tx, mut terminal_rx) = mpsc::channel::<Event>(128);
    let event_running = Arc::clone(&running);
    std::thread::spawn(move || {
        while event_running.load(Ordering::Relaxed) {
            if event::poll(Duration::from_millis(100)).unwrap_or(false)
                && let Ok(terminal_event) = event::read()
                && terminal_tx.blocking_send(terminal_event).is_err()
            {
                break;
            }
        }
    });

    let (domain_tx, mut domain_rx) = mpsc::channel(256);
    let mut domain_task = spawn_event_stream(Arc::clone(&client), &state, domain_tx.clone());
    let (backend_tx, mut backend_rx) = mpsc::channel(128);
    let watcher_tx = backend_tx.clone();
    let mut directory_watcher = new_debouncer(
        Duration::from_millis(350),
        move |result: DebounceEventResult| {
            if result.is_ok() {
                let _ = watcher_tx.try_send(BackendMessage::FilesystemChanged);
            }
        },
    )
    .ok();
    let mut watched_directory = None::<std::path::PathBuf>;
    let mut jobs_refresh = tokio::time::interval(Duration::from_secs(5));
    let mut monitor_refresh = tokio::time::interval(Duration::from_secs(2));
    let mut model_refresh = tokio::time::interval(Duration::from_secs(10));
    let mut preferences_refresh = tokio::time::interval(Duration::from_secs(5));
    let mut system = System::new_all();
    let mut metrics = runtime_metrics(&system, 0);
    let mut chat_previews = Vec::new();
    let mut media_previews = Vec::new();
    let mut preview_pending = BTreeSet::new();
    let mut media_preview_pending = BTreeSet::new();
    let mut preview_scope = PreviewScope::default();
    let mut media_preview_scope = MediaPreviewScope::default();
    let mut visual_inspector = VisualInspectorState::default();
    let mut hardware_profile = fallback_hardware_profile(&metrics);
    let mut requested_profile = performance_profile(state.model_manager.profile);
    spawn_hardware_profile_scan(Arc::clone(&client), requested_profile, backend_tx.clone());
    let mut observed_draft = String::new();
    let mut draft_warmup_requested = false;
    let mut warmup_deadline: Option<TokioInstant> = None;
    let mut saved_preferences = serde_json::to_vec(&state.preferences()).unwrap_or_default();
    if let Ok((models, roles)) =
        load_ollama_models(&model_api, state.active_workspace.as_deref()).await
    {
        metrics.loaded_models = models;
        metrics.model_roles = roles;
    }
    let result = async {
        loop {
            let draft = state.chat.question.value.trim().to_owned();
            if draft != observed_draft {
                observed_draft.clone_from(&draft);
                if draft.is_empty() {
                    draft_warmup_requested = false;
                }
                warmup_deadline = (draft.chars().count() >= 3
                    && !draft_warmup_requested
                    && state.chat.active_run.is_none()
                    && !state.chat.request_pending
                    && !state.jobs.values().any(|job| !is_terminal_job(&job.status)))
                .then(|| TokioInstant::now() + Duration::from_millis(500));
            }
            let animating = state.operation.active
                || state.chat.request_pending
                || state.chat.active_run.is_some()
                || state.jobs.values().any(|job| !is_terminal_job(&job.status));
            let redraw_delay = if animating {
                Duration::from_millis(200)
            } else {
                Duration::from_secs(2)
            };
            let warmup_at = warmup_deadline;
            let theme = Theme::at(state.theme_index);
            sync_directory_watcher(
                directory_watcher.as_mut(),
                &mut watched_directory,
                (state.overlay == Some(Overlay::FileBrowser))
                    .then(|| std::path::PathBuf::from(&state.file_browser.current_dir)),
            );
            let (terminal_width, terminal_height) =
                crossterm::terminal::size().unwrap_or((120, 34));
            let compact_inspector = terminal_width < 120 || terminal_height < 34;
            schedule_chat_previews(
                &state,
                &visual_inspector,
                !compact_inspector
                    || visual_inspector.tab == omarag_tui::VisualInspectorTab::Pages,
                Arc::clone(&client),
                &image_picker,
                &mut chat_previews,
                &mut preview_pending,
                &mut preview_scope,
                backend_tx.clone(),
            );
            schedule_media_previews(
                &visual_inspector,
                !compact_inspector
                    || visual_inspector.tab == omarag_tui::VisualInspectorTab::Figures,
                Arc::clone(&client),
                &image_picker,
                &mut media_previews,
                &mut media_preview_pending,
                &mut media_preview_scope,
                backend_tx.clone(),
            );
            let selected_profile = performance_profile(state.model_manager.profile);
            hardware_profile.profile = selected_profile;
            if requested_profile != selected_profile {
                requested_profile = selected_profile;
                spawn_hardware_recommendation(
                    Arc::clone(&client),
                    selected_profile,
                    backend_tx.clone(),
                );
            }
            terminal.draw(|frame| {
                render_with_runtime(
                    frame,
                    &state,
                    &theme,
                    &metrics,
                    &mut chat_previews,
                    &mut media_previews,
                    &visual_inspector,
                    &hardware_profile,
                )
            })?;
            tokio::select! {
                _ = tokio::time::sleep(redraw_delay) => {
                    if animating {
                        metrics.animation_tick = metrics.animation_tick.wrapping_add(1);
                    }
                }
                _ = async {
                    if let Some(at) = warmup_at {
                        tokio::time::sleep_until(at).await;
                    }
                }, if warmup_at.is_some() => {
                    warmup_deadline = None;
                    let draft = state.chat.question.value.trim().to_owned();
                    if draft == observed_draft
                        && draft.chars().count() >= 3
                        && let Some(workspace) = state.active_workspace.clone()
                    {
                        draft_warmup_requested = true;
                        spawn_command(
                            Arc::clone(&client),
                            model_api.clone(),
                            UiCommand::WarmupChat(workspace),
                            backend_tx.clone(),
                        );
                    }
                }
                _ = jobs_refresh.tick() => {
                    spawn_command(
                        Arc::clone(&client),
                        model_api.clone(),
                        UiCommand::RefreshJobs,
                        backend_tx.clone(),
                    );
                }
                _ = monitor_refresh.tick() => {
                    Theme::refresh_omarchy();
                    system.refresh_cpu_usage();
                    system.refresh_memory();
                    let loaded_models = std::mem::take(&mut metrics.loaded_models);
                    let model_roles = std::mem::take(&mut metrics.model_roles);
                    metrics = runtime_metrics(&system, metrics.animation_tick);
                    metrics.loaded_models = loaded_models;
                    metrics.model_roles = model_roles;
                }
                _ = model_refresh.tick() => {
                    if let Ok((models, roles)) = load_ollama_models(
                        &model_api,
                        state.active_workspace.as_deref(),
                    ).await {
                        metrics.loaded_models = models;
                        metrics.model_roles = roles;
                    }
                }
                _ = preferences_refresh.tick() => {
                    // Persist the theme by name; the index is only a cache.
                    state.theme_name = Theme::at(state.theme_index).name.to_owned();
                    let preferences = state.preferences();
                    if let Ok(encoded) = serde_json::to_vec_pretty(&preferences)
                        && encoded != saved_preferences
                        && save_preferences(&preferences_path, &encoded).is_ok()
                    {
                        saved_preferences = encoded;
                    }
                }
                Some(domain_event) = domain_rx.recv() => {
                    let completed_chat = (domain_event.event_type == "run.completed").then(|| domain_event.timestamp.clone());
                    let completed_visual = (domain_event.event_type == "run.completed")
                        .then(|| domain_event.run_id.clone())
                        .flatten();
                    let refresh_jobs = domain_event.event_type.starts_with("job.");
                    let refresh_features = matches!(
                        domain_event.event_type.as_str(),
                        "job.completed"
                            | "job.failed"
                            | "document.changed"
                            | "source.changed"
                            | "config.changed"
                            | "backup.completed"
                    );
                    update(&mut state, Action::EventReceived(domain_event));
                    if let Some(timestamp) = completed_chat {
                        remember_chat_session(&mut state, timestamp);
                    }
                    if let Some(run_id) = completed_visual {
                        spawn_visual_evidence(
                            Arc::clone(&client),
                            run_id,
                            backend_tx.clone(),
                        );
                    }
                    if refresh_jobs {
                        spawn_command(
                            Arc::clone(&client),
                            model_api.clone(),
                            UiCommand::RefreshJobs,
                            backend_tx.clone(),
                        );
                    }
                    if refresh_features && let Some(workspace) = state.active_workspace.clone() {
                        spawn_command(
                            Arc::clone(&client),
                            model_api.clone(),
                            UiCommand::RefreshWorkspaceFeatures(workspace),
                            backend_tx.clone(),
                        );
                    }
                }
                Some(terminal_event) = terminal_rx.recv() => {
                    if let Some(command) = handle_event_with_visuals(
                        &mut state,
                        &mut visual_inspector,
                        terminal_event,
                    ) {
                        spawn_command(
                            Arc::clone(&client),
                            model_api.clone(),
                            command,
                            backend_tx.clone(),
                        );
                    }
                }
                Some(message) = backend_rx.recv() => {
                    if matches!(&message, BackendMessage::RunStarted(Ok(_))) {
                        visual_inspector.clear();
                        media_previews.clear();
                        media_preview_pending.clear();
                        media_preview_scope.token.cancel();
                        media_preview_scope = MediaPreviewScope::default();
                    }
                    let message = match message {
                        BackendMessage::FilesystemChanged => {
                            if state.overlay == Some(Overlay::FileBrowser) {
                                refresh_file_browser(&mut state);
                            }
                            continue;
                        }
                        BackendMessage::PreviewLoaded { key, result } => {
                            preview_pending.remove(&key);
                            if let Ok(preview) = result {
                                chat_previews.retain(|item| (item.pdf_path.as_str(), item.page) != (key.0.as_str(), key.1));
                                chat_previews.push(preview);
                                chat_previews.sort_by_key(|item| citation_preview_position(
                                    &state,
                                    &visual_inspector,
                                    &item.pdf_path,
                                    item.page,
                                ));
                            }
                            continue;
                        }
                        BackendMessage::MediaPreviewLoaded { key, result } => {
                            media_preview_pending.remove(&key);
                            if let Ok(preview) = result {
                                media_previews.retain(|item| item.media_id != preview.media_id);
                                media_previews.push(preview);
                                media_previews.sort_by_key(|item| {
                                    visual_inspector
                                        .evidence
                                        .media
                                        .iter()
                                        .position(|asset| asset.media_id == item.media_id)
                                        .unwrap_or(usize::MAX)
                                });
                            }
                            continue;
                        }
                        BackendMessage::VisualEvidenceLoaded { run_id, response } => {
                            if state.chat.last_run.as_ref() == Some(&run_id) {
                                match response {
                                    Some(response) => visual_inspector.replace(run_id, response),
                                    None => visual_inspector.use_legacy(run_id),
                                }
                                media_previews.clear();
                                media_preview_pending.clear();
                                media_preview_scope.token.cancel();
                                media_preview_scope = MediaPreviewScope::default();
                            }
                            continue;
                        }
                        BackendMessage::HardwareProfileLoaded(Some(profile)) => {
                            hardware_profile = merge_hardware_profile(
                                hardware_profile,
                                profile,
                                performance_profile(state.model_manager.profile),
                            );
                            continue;
                        }
                        BackendMessage::HardwareProfileLoaded(None) => continue,
                        BackendMessage::HardwareRecommendationLoaded { profile, response } => {
                            if profile == performance_profile(state.model_manager.profile)
                                && let Some(response) = response
                            {
                                hardware_profile.profile = profile;
                                if !response.catalog_version.is_empty() {
                                    hardware_profile.catalog_version = response.catalog_version;
                                }
                                hardware_profile.expert_mode = response.expert_mode;
                                hardware_profile.recommendations = response.recommendations;
                            }
                            continue;
                        }
                        message => message,
                    };
                    if apply_backend_message(&mut state, message) {
                        domain_task.abort();
                        domain_task = spawn_event_stream(
                            Arc::clone(&client),
                            &state,
                            domain_tx.clone(),
                        );
                        if let Some(workspace) = state.active_workspace.clone() {
                            spawn_command(
                                Arc::clone(&client),
                                model_api.clone(),
                                UiCommand::RefreshWorkspaceFeatures(workspace),
                                backend_tx.clone(),
                            );
                        }
                    }
                }
                _ = tokio::signal::ctrl_c() => {
                    if state.overlay == Some(Overlay::ConfirmQuit) {
                        update(&mut state, Action::QuitRequested);
                    } else {
                        state.overlay = Some(Overlay::ConfirmQuit);
                        state.input_mode = InputMode::Nav;
                    }
                }
            }
            if state.quit_requested {
                break;
            }
        }
        Ok::<(), anyhow::Error>(())
    }
    .await;
    if let Ok(encoded) = serde_json::to_vec_pretty(&state.preferences()) {
        let _ = save_preferences(&preferences_path, &encoded);
    }
    running.store(false, Ordering::Relaxed);
    preview_scope.token.cancel();
    media_preview_scope.token.cancel();
    domain_task.abort();
    let paste_result = execute!(stdout(), DisableMouseCapture, DisableBracketedPaste);
    let restore_result = ratatui::try_restore();
    paste_result.context("Could not disable bracketed paste")?;
    restore_result.context("Could not restore terminal")?;
    result
}

fn sync_directory_watcher(
    debouncer: Option<
        &mut notify_debouncer_mini::Debouncer<notify_debouncer_mini::notify::RecommendedWatcher>,
    >,
    watched: &mut Option<std::path::PathBuf>,
    desired: Option<std::path::PathBuf>,
) {
    if *watched == desired {
        return;
    }
    let Some(debouncer) = debouncer else {
        *watched = None;
        return;
    };
    if let Some(previous) = watched.take() {
        let _ = debouncer.watcher().unwatch(&previous);
    }
    if let Some(directory) = desired.filter(|path| path.is_dir())
        && debouncer
            .watcher()
            .watch(&directory, RecursiveMode::NonRecursive)
            .is_ok()
    {
        *watched = Some(directory);
    }
}

fn runtime_metrics(system: &System, animation_tick: u64) -> RuntimeMetrics {
    let gpu = gpu_metrics();
    RuntimeMetrics {
        cpu_usage: system.global_cpu_usage(),
        cpu_count: system.cpus().len(),
        memory_used: system.used_memory(),
        memory_total: system.total_memory(),
        memory_available: system.available_memory(),
        gpu_name: gpu.name,
        vram_used: gpu.vram_used,
        vram_total: gpu.vram_total,
        shared_gpu_memory: gpu.shared_memory,
        animation_tick,
        loaded_models: Vec::new(),
        model_roles: Vec::new(),
    }
}

fn is_terminal_job(status: &omarag_domain::JobStatus) -> bool {
    matches!(
        status,
        omarag_domain::JobStatus::Completed
            | omarag_domain::JobStatus::Paused
            | omarag_domain::JobStatus::Cancelled
            | omarag_domain::JobStatus::Failed
    )
}

#[derive(Default)]
struct GpuMetrics {
    name: Option<String>,
    vram_used: u64,
    vram_total: u64,
    shared_memory: u64,
}

fn gpu_metrics() -> GpuMetrics {
    let Ok(cards) = std::fs::read_dir("/sys/class/drm") else {
        return GpuMetrics::default();
    };
    for card in cards.filter_map(Result::ok) {
        let card_name = card.file_name().to_string_lossy().into_owned();
        let Some(index) = card_name.strip_prefix("card") else {
            continue;
        };
        if index.is_empty() || !index.chars().all(|character| character.is_ascii_digit()) {
            continue;
        }
        let device = card.path().join("device");
        let vendor = read_sysfs_hex(device.join("vendor"));
        let device_id = read_sysfs_hex(device.join("device"));
        let name = match (vendor, device_id) {
            (Some(0x1002), Some(0x1900)) => "AMD Radeon 760M",
            (Some(0x1002), _) => "AMD Radeon",
            (Some(0x10de), _) => "NVIDIA GPU",
            (Some(0x8086), _) => "Intel Graphics",
            _ => "Graphics adapter",
        };
        return GpuMetrics {
            name: Some(name.into()),
            vram_used: read_sysfs_u64(device.join("mem_info_vram_used")).unwrap_or(0),
            vram_total: read_sysfs_u64(device.join("mem_info_vram_total")).unwrap_or(0),
            shared_memory: read_sysfs_u64(device.join("mem_info_gtt_total")).unwrap_or(0),
        };
    }
    GpuMetrics::default()
}

fn read_sysfs_u64(path: impl AsRef<std::path::Path>) -> Option<u64> {
    std::fs::read_to_string(path).ok()?.trim().parse().ok()
}

fn read_sysfs_hex(path: impl AsRef<std::path::Path>) -> Option<u64> {
    let value = std::fs::read_to_string(path).ok()?;
    u64::from_str_radix(value.trim().trim_start_matches("0x"), 16).ok()
}

async fn load_ollama_models(
    api: &ModelApi,
    workspace: Option<&str>,
) -> Result<(Vec<LoadedModel>, Vec<ModelRoleStatus>)> {
    let mut request = api.request(reqwest::Method::GET, "/v1/models/runtime")?;
    if let Some(workspace) = workspace {
        request = request.query(&[("workspace_id", workspace)]);
    }
    let response = request
        .timeout(Duration::from_secs(1))
        .send()
        .await?
        .error_for_status()?
        .json::<OllamaProcesses>()
        .await?;
    let models = response
        .models
        .into_iter()
        .map(|model| LoadedModel {
            name: model.name,
            size: model.size,
            size_vram: model.size_vram,
            context_length: model.context_length,
            parameter_size: model.parameter_size,
            quantization: model.quantization_level,
        })
        .collect();
    let roles = response
        .roles
        .into_iter()
        .map(|role| ModelRoleStatus {
            role: role.role,
            model: role.model,
            residency: role.residency,
            shared_with: role.shared_with,
        })
        .collect();
    Ok((models, roles))
}

async fn load_model_catalog(
    api: &ModelApi,
    source: ModelSource,
    category: ModelCategory,
    query: &str,
    quantization: &str,
    context_tokens: u32,
    profile: HardwareProfile,
) -> Result<ModelCatalogResponse> {
    let mut url = api.base_url.join("v1/models/catalog")?;
    {
        let mut pairs = url.query_pairs_mut();
        pairs
            .append_pair("source", source.api_label())
            .append_pair("category", category.api_label())
            .append_pair("query", query)
            .append_pair("quantization", quantization)
            .append_pair("context_tokens", &context_tokens.to_string())
            .append_pair("profile", profile.api_label());
    }
    let mut request = api.client.get(url);
    if let Some(token) = &api.token {
        request = request.bearer_auth(token);
    }
    Ok(request
        .timeout(Duration::from_secs(35))
        .send()
        .await?
        .error_for_status()?
        .json::<ModelCatalogResponse>()
        .await?)
}

async fn pull_model(
    api: &ModelApi,
    model: String,
    tx: mpsc::Sender<BackendMessage>,
) -> Result<(), String> {
    let result = stream_model_install(api, "/v1/models/pull", &model, None, tx.clone()).await;
    let _ = tx
        .send(BackendMessage::ModelOperationFinished {
            model,
            operation: ModelOperation::Download,
            result: result.clone(),
        })
        .await;
    result
}

async fn stream_model_install(
    api: &ModelApi,
    path: &str,
    model: &str,
    transfer_label: Option<&str>,
    tx: mpsc::Sender<BackendMessage>,
) -> Result<(), String> {
    async {
        let mut response = api
            .request(reqwest::Method::POST, path)?
            .json(&serde_json::json!({ "model": model }))
            .send()
            .await?
            .error_for_status()?;
        let mut pending = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            pending.extend_from_slice(&chunk);
            while let Some(end) = pending.iter().position(|byte| *byte == b'\n') {
                let line = pending.drain(..=end).collect::<Vec<_>>();
                let progress = serde_json::from_slice::<PullProgress>(&line)?;
                if let Some(error) = progress.error {
                    anyhow::bail!(error);
                }
                let _ = tx
                    .send(BackendMessage::ModelTransfer(ModelTransfer {
                        model: transfer_label.unwrap_or(model).to_owned(),
                        status: progress.status,
                        completed: progress.completed,
                        total: progress.total,
                    }))
                    .await;
            }
        }
        Ok::<(), anyhow::Error>(())
    }
    .await
    .map_err(|error| error.to_string())
}

async fn import_gguf(
    api: &ModelApi,
    path: String,
    model: String,
    category: ModelCategory,
    tx: mpsc::Sender<BackendMessage>,
) {
    let result = async {
        let form = reqwest::multipart::Form::new()
            .text("model", model.clone())
            .text("category", category.api_label().to_owned())
            .file("file", &path)
            .await?;
        let mut response = api
            .request(reqwest::Method::POST, "/v1/models/import/gguf")?
            .multipart(form)
            .send()
            .await?
            .error_for_status()?;
        let mut pending = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            pending.extend_from_slice(&chunk);
            while let Some(end) = pending.iter().position(|byte| *byte == b'\n') {
                let line = pending.drain(..=end).collect::<Vec<_>>();
                let progress = serde_json::from_slice::<PullProgress>(&line)?;
                if let Some(error) = progress.error {
                    anyhow::bail!(error);
                }
                let _ = tx
                    .send(BackendMessage::ModelTransfer(ModelTransfer {
                        model: model.clone(),
                        status: progress.status,
                        completed: progress.completed,
                        total: progress.total,
                    }))
                    .await;
            }
        }
        Ok::<(), anyhow::Error>(())
    }
    .await
    .map_err(|error| error.to_string());
    let _ = tx
        .send(BackendMessage::ModelOperationFinished {
            model,
            operation: ModelOperation::Download,
            result,
        })
        .await;
}

async fn bootstrap(client: &dyn OmaRagClient, state: &mut AppState) {
    match client.meta().await {
        Ok(meta) => {
            let backend_version = meta.omarag_version.clone();
            update(state, Action::BackendConnected(meta));
            if backend_version != env!("CARGO_PKG_VERSION") {
                update(
                    state,
                    Action::Notify(Notification {
                        level: NotificationLevel::Warning,
                        message: format!(
                            "Version mismatch: TUI {} · API {backend_version}. Restart the daemon after updating.",
                            env!("CARGO_PKG_VERSION")
                        ),
                    }),
                );
            }
            match client.list_workspaces().await {
                Ok(workspaces) => {
                    update(state, Action::WorkspacesLoaded(workspaces));
                }
                Err(error) => disconnect(state, error.to_string()),
            }
        }
        Err(error) => disconnect(state, error.to_string()),
    }
    if let Ok(jobs) = client.list_jobs(state.active_workspace.clone()).await {
        update(state, Action::JobsLoaded(jobs));
    }
    if let Some(workspace) = state.active_workspace.clone()
        && let Ok(features) = load_workspace_features(client, workspace).await
    {
        apply_workspace_features(state, *features);
    }
}

async fn load_workspace_features(
    client: &dyn OmaRagClient,
    workspace: WorkspaceId,
) -> Result<Box<WorkspaceFeatures>, String> {
    let (documents, sources, quality, backups, config) = tokio::join!(
        client.list_documents(workspace.clone()),
        client.list_sources(workspace.clone()),
        client.quality(workspace.clone()),
        client.list_backups(workspace.clone()),
        client.config(workspace),
    );
    let documents = documents.map_err(|error| error.to_string())?;
    let details = documents
        .iter()
        .map(|document| {
            (
                document.id.clone(),
                DocumentInsight {
                    size_bytes: document.size_bytes,
                    pages: document.page_count,
                    sha256: document.fingerprint.clone(),
                    chunks: document
                        .pipeline_stats
                        .get("chunks")
                        .and_then(serde_json::Value::as_u64),
                },
            )
        })
        .collect();
    Ok(Box::new(WorkspaceFeatures {
        documents,
        sources: sources.map_err(|error| error.to_string())?,
        quality: quality.map_err(|error| error.to_string())?,
        backups: backups.map_err(|error| error.to_string())?,
        config: config.map_err(|error| error.to_string())?,
        details,
    }))
}

fn apply_workspace_features(state: &mut AppState, features: WorkspaceFeatures) {
    state.library.details = features.details;
    update(
        state,
        Action::WorkspaceFeaturesLoaded {
            documents: features.documents,
            sources: features.sources,
            quality: features.quality,
            backups: features.backups,
            config: features.config,
        },
    );
}

fn disconnect(state: &mut AppState, message: String) {
    update(state, Action::BackendDisconnected(message.clone()));
    notify_error(state, message);
}

fn notify_error(state: &mut AppState, message: String) {
    update(
        state,
        Action::Notify(Notification {
            level: NotificationLevel::Error,
            message,
        }),
    );
}

fn spawn_event_stream(
    client: Arc<HttpOmaRagClient>,
    state: &AppState,
    event_tx: mpsc::Sender<DomainEvent>,
) -> tokio::task::JoinHandle<()> {
    let workspace_id = state.active_workspace.clone();
    let last_event_id = state.last_event_id;
    tokio::spawn(async move {
        let mut cursor = last_event_id;
        loop {
            let subscription = EventSubscription {
                workspace_id: workspace_id.clone(),
                job_id: None,
                run_id: None,
                last_event_id: cursor,
            };
            if let Ok(mut stream) = client.subscribe_events(subscription).await {
                while let Some(event) = stream.next().await {
                    match event {
                        Ok(event) => {
                            cursor = Some(event.event_id);
                            if event_tx.send(event).await.is_err() {
                                return;
                            }
                        }
                        Err(_) => break,
                    }
                }
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    })
}

fn spawn_visual_evidence(
    client: Arc<HttpOmaRagClient>,
    run_id: RunId,
    tx: mpsc::Sender<BackendMessage>,
) {
    tokio::spawn(async move {
        let response = match client.visual_evidence(run_id.clone()).await {
            Ok(response) => Some(response),
            Err(OmaRagError::Api { status: 404, .. }) => None,
            // A malformed/older response must degrade exactly like a legacy
            // server: cited pages remain usable and figures stay empty.
            Err(_) => None,
        };
        let _ = tx
            .send(BackendMessage::VisualEvidenceLoaded { run_id, response })
            .await;
    });
}

fn spawn_hardware_profile_scan(
    client: Arc<HttpOmaRagClient>,
    profile: PerformanceProfile,
    tx: mpsc::Sender<BackendMessage>,
) {
    tokio::spawn(async move {
        let scan = tokio::time::timeout(Duration::from_secs(3), client.hardware_scan())
            .await
            .ok()
            .and_then(Result::ok);
        let Some(mut scan) = scan else {
            let _ = tx.send(BackendMessage::HardwareProfileLoaded(None)).await;
            return;
        };
        if let Some(recommendation) = load_model_recommendation(client.as_ref(), profile).await {
            if !recommendation.catalog_version.is_empty() {
                scan.catalog_version = recommendation.catalog_version;
            }
            if !recommendation.tier_label.is_empty() {
                scan.tier_label = recommendation.tier_label;
            }
            scan.profile = profile;
            scan.expert_mode = recommendation.expert_mode;
            scan.recommendations = recommendation.recommendations;
        }
        let _ = tx
            .send(BackendMessage::HardwareProfileLoaded(Some(scan)))
            .await;
    });
}

fn spawn_hardware_recommendation(
    client: Arc<HttpOmaRagClient>,
    profile: PerformanceProfile,
    tx: mpsc::Sender<BackendMessage>,
) {
    tokio::spawn(async move {
        let response = load_model_recommendation(client.as_ref(), profile).await;
        let _ = tx
            .send(BackendMessage::HardwareRecommendationLoaded { profile, response })
            .await;
    });
}

async fn load_model_recommendation(
    client: &HttpOmaRagClient,
    profile: PerformanceProfile,
) -> Option<HardwareProfileResponse> {
    // GET is the compact, read-only TUI view. The richer POST response is a
    // preflight envelope and must never be mistaken for an apply operation.
    tokio::time::timeout(Duration::from_secs(3), client.model_recommendation(profile))
        .await
        .ok()
        .and_then(Result::ok)
}

fn merge_hardware_profile(
    mut fallback: HardwareProfileResponse,
    backend: HardwareProfileResponse,
    selected_profile: PerformanceProfile,
) -> HardwareProfileResponse {
    let recommendations_match = backend.profile == selected_profile;
    // Any successfully decoded scan is authoritative for tier and bottleneck.
    fallback.schema_version = backend.schema_version;
    fallback.tier = backend.tier;
    if !backend.tier_label.is_empty() {
        fallback.tier_label = backend.tier_label;
    }
    if !backend.limiting_factor.is_empty() {
        fallback.limiting_factor = backend.limiting_factor;
    }
    if !backend.catalog_version.is_empty() {
        fallback.catalog_version = backend.catalog_version;
    }
    fallback.scanned_at = backend.scanned_at;
    fallback.profile = selected_profile;
    fallback.expert_mode = backend.expert_mode;
    if recommendations_match {
        fallback.recommendations = backend.recommendations;
    }
    fallback
}

fn spawn_command(
    client: Arc<HttpOmaRagClient>,
    model_api: ModelApi,
    command: UiCommand,
    tx: mpsc::Sender<BackendMessage>,
) {
    tokio::spawn(async move {
        let message = match command {
            UiCommand::OpenWorkspace(id) => {
                let result = client
                    .open_workspace(id.clone())
                    .await
                    .map(|_| id)
                    .map_err(|error| error.to_string());
                BackendMessage::WorkspaceOpened(result)
            }
            UiCommand::CreateWorkspace(request) => BackendMessage::WorkspaceCreated(
                client
                    .create_workspace(request)
                    .await
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::DeleteLibrary { id, physical } => {
                let result = client
                    .delete_workspace(id.clone(), physical)
                    .await
                    .map_err(|error| error.to_string());
                BackendMessage::LibraryDeleted {
                    id,
                    physical,
                    result,
                }
            }
            UiCommand::StartRun {
                workspace,
                session_id,
                question,
                evidence_mode,
                profile,
                filters,
            } => BackendMessage::RunStarted(
                client
                    .start_run(workspace, {
                        let mut request = RunRequest::question(question, evidence_mode)
                            .with_session_id(session_id);
                        request.filters = filters;
                        request.options.profile = profile.api_label().into();
                        request
                    })
                    .await
                    .map(|run| run.id)
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::WarmupChat(workspace) => {
                if let Ok(request) = model_api.request(
                    reqwest::Method::POST,
                    &format!("/v1/workspaces/{workspace}/runtime/warmup"),
                ) {
                    let _ = request.timeout(Duration::from_secs(30)).send().await;
                }
                BackendMessage::WarmupFinished
            }
            UiCommand::CancelRun(run_id) => BackendMessage::RunCancelled(
                client
                    .cancel_run(run_id)
                    .await
                    .map(|_| ())
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::Search { workspace, request } => BackendMessage::SearchCompleted(
                client
                    .explain_search(workspace, request)
                    .await
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::Ingest {
                workspace,
                request,
                preflight_id,
            } => {
                let result = async {
                    let (preflight_id, sources) = if let Some(preflight_id) = preflight_id {
                        (preflight_id, request.sources)
                    } else {
                        let preflight = client
                            .preflight_import(
                                workspace.clone(),
                                PreflightImportRequest {
                                    sources: request.sources.clone(),
                                },
                            )
                            .await?;
                        let mut sources = request.sources;
                        for (source, candidate) in sources.iter_mut().zip(preflight.candidates) {
                            source.path = candidate.source;
                            source.fingerprint = Some(candidate.fingerprint);
                            source.candidate_id = Some(candidate.id);
                            source.metadata = Some(omarag_domain::BookMetadata {
                                confirmed: true,
                                ..candidate.metadata
                            });
                        }
                        (preflight.id, sources)
                    };
                    client
                        .commit_import(
                            workspace,
                            CommitImportRequest {
                                preflight_id,
                                sources,
                                processing_profile: request.processing_profile,
                                duplicate_policy: request.duplicate_policy,
                                validity_policy: request.validity_policy,
                                indexing: request.indexing,
                            },
                            Uuid::new_v4().to_string(),
                        )
                        .await
                        .map(|accepted| accepted.id)
                }
                .await;
                BackendMessage::ImportAccepted(result.map_err(|error| error.to_string()))
            }
            UiCommand::Job { id, command } => {
                let result = match command {
                    JobCommand::Pause => client.pause_job(id).await,
                    JobCommand::Resume => client.resume_job(id).await,
                    JobCommand::Cancel => client.cancel_job(id).await,
                };
                BackendMessage::JobUpdated(result.map(|_| ()).map_err(|error| error.to_string()))
            }
            UiCommand::RefreshJobs => BackendMessage::JobsLoaded(
                client
                    .list_jobs(None)
                    .await
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::RefreshWorkspaceFeatures(workspace) => {
                BackendMessage::WorkspaceFeaturesLoaded(
                    load_workspace_features(client.as_ref(), workspace).await,
                )
            }
            UiCommand::CreateBackup(workspace) => BackendMessage::BackupCreated(
                client
                    .create_backup(workspace)
                    .await
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::CreateSource { workspace, request } => BackendMessage::SourceCreated(
                client
                    .create_source(workspace, request)
                    .await
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::SaveConfig {
                workspace,
                request,
                etag,
            } => BackendMessage::ConfigSaved(
                client
                    .update_config(workspace, request, etag)
                    .await
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::PreflightAutomaticStack { workspace, profile } => {
                let result = client
                    .preflight_model_profile(workspace.clone(), profile)
                    .await
                    .map_err(|error| error.to_string());
                BackendMessage::AutomaticStackPreflight { workspace, result }
            }
            UiCommand::ApplyAutomaticStack { workspace, request } => {
                BackendMessage::AutomaticStackApplied(
                    client
                        .apply_model_profile(workspace, request)
                        .await
                        .map_err(|error| error.to_string()),
                )
            }
            UiCommand::RefreshModelCatalog {
                source,
                category,
                query,
                quantization,
                context_tokens,
                profile,
            } => BackendMessage::ModelCatalogLoaded(
                load_model_catalog(
                    &model_api,
                    source,
                    category,
                    &query,
                    &quantization,
                    context_tokens,
                    profile,
                )
                .await
                .map_err(|error| error.to_string()),
            ),
            UiCommand::PullModel { model } => {
                let _ = pull_model(&model_api, model, tx.clone()).await;
                return;
            }
            UiCommand::PullPackage {
                name,
                models,
                hugging_face_models,
                workspace,
                defaults,
                vector_dim,
            } => {
                let install_count = models.len() + hugging_face_models.len();
                let status = if install_count == 0 {
                    "package already installed · preparing activation".into()
                } else {
                    format!("installing selected package · {install_count} artifacts")
                };
                let _ = tx
                    .send(BackendMessage::ModelTransfer(ModelTransfer {
                        model: name.clone(),
                        status,
                        completed: 0,
                        total: 0,
                    }))
                    .await;
                let mut installed_models = Vec::new();
                let mut result = Ok(None);
                for model in models {
                    let label = format!("Package {name} · {model}");
                    if let Err(error) = stream_model_install(
                        &model_api,
                        "/v1/models/pull",
                        &model,
                        Some(&label),
                        tx.clone(),
                    )
                    .await
                    {
                        result = Err(error);
                        break;
                    }
                    installed_models.push(model);
                }
                for model in hugging_face_models {
                    if result.is_err() {
                        break;
                    }
                    let label = format!("Package {name} · {model}");
                    if let Err(error) = stream_model_install(
                        &model_api,
                        "/v1/models/install-hugging-face",
                        &model,
                        Some(&label),
                        tx.clone(),
                    )
                    .await
                    {
                        result = Err(error);
                        break;
                    }
                    installed_models.push(model);
                }
                let mut activation_status = if workspace.is_some() {
                    "installed · preparing activation".to_owned()
                } else {
                    "installed · choose a library later to activate it".to_owned()
                };
                if result.is_ok() {
                    let role = |wanted: ModelCategory| {
                        defaults
                            .iter()
                            .find(|item| item.role == wanted)
                            .map(|item| item.model.clone())
                            .unwrap_or_default()
                    };
                    let payload = serde_json::json!({
                        "chat": role(ModelCategory::Chat),
                        "vl": role(ModelCategory::Vl),
                        "embedding": role(ModelCategory::Embedding),
                        "rerank": role(ModelCategory::Rerank),
                        "embedding_provider": "ollama",
                        "rerank_provider": "cross-encoder",
                        "vector_dim": vector_dim,
                    });
                    let apply = async {
                        let Some(workspace) = workspace else {
                            return Ok::<(Option<ConfigDocument>, String), anyhow::Error>((
                                None,
                                "installed · choose a library later to activate it".into(),
                            ));
                        };
                        let preflight = model_api
                            .request(
                                reqwest::Method::POST,
                                &format!("/v1/workspaces/{workspace}/model-defaults/preflight"),
                            )?
                            .json(&payload)
                            .send()
                            .await?
                            .error_for_status()?;
                        let preflight: ModelDefaultsPreflightResponse = preflight.json().await?;
                        if preflight.requires_reindex {
                            return Ok((
                                None,
                                "installed · activation requires a full library reindex".into(),
                            ));
                        }
                        let current = model_api
                            .request(
                                reqwest::Method::GET,
                                &format!("/v1/workspaces/{workspace}/config"),
                            )?
                            .send()
                            .await?
                            .error_for_status()?
                            .json::<ConfigDocument>()
                            .await?;
                        let response = model_api
                            .request(
                                reqwest::Method::POST,
                                &format!("/v1/workspaces/{workspace}/model-defaults/apply"),
                            )?
                            .header("If-Match", current.etag)
                            .json(&payload)
                            .send()
                            .await?
                            .error_for_status()?;
                        Ok::<(Option<ConfigDocument>, String), anyhow::Error>((
                            Some(response.json().await?),
                            "installed and activated for the current library".into(),
                        ))
                    }
                    .await;
                    match apply {
                        Ok((config, status)) => {
                            result = Ok(config);
                            activation_status = status;
                        }
                        Err(error) => result = Err(error.to_string()),
                    }
                }
                let _ = tx
                    .send(BackendMessage::ModelPackageFinished {
                        name,
                        installed_models,
                        activation_status,
                        result,
                    })
                    .await;
                return;
            }
            UiCommand::ImportGguf {
                path,
                model,
                category,
            } => {
                import_gguf(&model_api, path, model, category, tx.clone()).await;
                return;
            }
            UiCommand::PreloadModel {
                model,
                context_tokens,
                keep_alive,
            } => {
                let result = model_api
                    .request(reqwest::Method::POST, "/v1/models/load")
                    .map(|request| {
                        request.json(&serde_json::json!({
                            "model": model,
                            "keep_alive": keep_alive,
                            "context_tokens": context_tokens,
                        }))
                    })
                    .map_err(|error| error.to_string());
                let result = match result {
                    Ok(request) => model_operation_result(request.send().await).await,
                    Err(error) => Err(error),
                };
                BackendMessage::ModelOperationFinished {
                    model,
                    operation: ModelOperation::Load,
                    result,
                }
            }
            UiCommand::UnloadModel { model } => {
                let result = match model_api.request(reqwest::Method::POST, "/v1/models/unload") {
                    Ok(request) => {
                        model_operation_result(
                            request
                                .json(&serde_json::json!({ "model": model }))
                                .send()
                                .await,
                        )
                        .await
                    }
                    Err(error) => Err(error.to_string()),
                };
                BackendMessage::ModelOperationFinished {
                    model,
                    operation: ModelOperation::Unload,
                    result,
                }
            }
            UiCommand::DeleteModel { model, confirm } => {
                let result = match model_api.request(reqwest::Method::DELETE, "/v1/models") {
                    Ok(request) => {
                        model_operation_result(
                            request
                                .json(&serde_json::json!({ "model": model, "confirm": confirm }))
                                .send()
                                .await,
                        )
                        .await
                    }
                    Err(error) => Err(error.to_string()),
                };
                BackendMessage::ModelOperationFinished {
                    model,
                    operation: ModelOperation::Delete,
                    result,
                }
            }
            UiCommand::OpenPdf { path, page } => {
                BackendMessage::ExternalOpened(open_pdf(&path, page))
            }
            UiCommand::OpenPageImage {
                path,
                page,
                primary_anchors,
                context_anchors,
            } => BackendMessage::ExternalOpened(open_pdf_page_image(
                &path,
                page,
                &primary_anchors,
                &context_anchors,
            )),
            UiCommand::AnalyzeImport {
                workspace,
                selected,
                existing,
            } => {
                let mut result =
                    tokio::task::spawn_blocking(move || analyze_import(&selected, &existing))
                        .await
                        .unwrap_or_else(|error| ImportPreflight {
                            error: Some(error.to_string()),
                            ..ImportPreflight::default()
                        });
                if result.error.is_none() && !result.pdfs.is_empty() {
                    match client
                        .preflight_import(
                            workspace,
                            PreflightImportRequest {
                                sources: omarag_domain::IngestRequest::files(result.pdfs.clone())
                                    .sources,
                            },
                        )
                        .await
                    {
                        Ok(batch) => {
                            result.server_preflight_id = Some(batch.id);
                            result.books = batch
                                .candidates
                                .into_iter()
                                .map(|candidate| PendingBookReview {
                                    candidate_id: candidate.id,
                                    source: candidate.source,
                                    fingerprint: candidate.fingerprint,
                                    metadata: candidate.metadata,
                                    issues: candidate.issues,
                                })
                                .collect();
                        }
                        Err(error) => result.error = Some(error.to_string()),
                    }
                }
                BackendMessage::ImportAnalyzed(result)
            }
            UiCommand::DeleteDocument {
                workspace,
                document,
            } => BackendMessage::DocumentDeleted(
                client
                    .delete_document(workspace, document.id.clone())
                    .await
                    .map(|_| document)
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::RestoreDocument {
                workspace,
                document,
            } => {
                let result = async {
                    client
                        .restore_document(workspace, document.id.clone())
                        .await
                        .map_err(|error| error.to_string())?;
                    Ok(document)
                }
                .await;
                BackendMessage::DocumentRestored(result)
            }
            UiCommand::ExportChat { workspace, session } => {
                BackendMessage::ExternalOpened(export_chat(&workspace, &session))
            }
            UiCommand::CopyText(value) => BackendMessage::ClipboardCopied {
                selection: false,
                characters: value.chars().count(),
                result: copy_text(&value),
            },
            UiCommand::CopySelection(value) => BackendMessage::ClipboardCopied {
                selection: true,
                characters: value.chars().count(),
                result: copy_text(&value),
            },
        };
        let _ = tx.send(message).await;
    });
}

fn open_pdf(path: &str, page: Option<u32>) -> Result<(), String> {
    if !std::path::Path::new(path).exists() {
        return Err(format!("PDF no longer exists: {path}"));
    }
    if let Ok(template) = std::env::var("OMARAG_PDF_VIEWER") {
        let mut parts = template.split_whitespace();
        if let Some(program) = parts.next() {
            let page = page.unwrap_or(1).to_string();
            let args = parts
                .map(|argument| argument.replace("%f", path).replace("%p", &page))
                .collect::<Vec<_>>();
            if Command::new(program).args(args).spawn().is_ok() {
                return Ok(());
            }
        }
    }
    if let Some(page) = page {
        let page_text = page.to_string();
        let zero_based = page.saturating_sub(1).to_string();
        for (viewer, args) in [
            ("evince", vec!["--page-index", zero_based.as_str(), path]),
            ("okular", vec!["-p", page_text.as_str(), path]),
            ("zathura", vec!["-P", page_text.as_str(), path]),
        ] {
            if Command::new(viewer).args(args).spawn().is_ok() {
                return Ok(());
            }
        }
    }
    let target = page
        .and_then(|page| {
            Url::from_file_path(path).ok().map(|mut url| {
                url.set_fragment(Some(&format!("page={page}")));
                url.to_string()
            })
        })
        .unwrap_or_else(|| path.to_owned());
    Command::new("xdg-open")
        .arg(target)
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Could not open PDF: {error}"))
}

fn open_pdf_page_image(
    path: &str,
    page: u32,
    primary: &[omarag_domain::CitationAnchor],
    context: &[omarag_domain::CitationAnchor],
) -> Result<(), String> {
    let image = render_pdf_page_with_anchors(path, page, primary, context)?;
    Command::new("xdg-open")
        .arg(&image)
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Could not open page image: {error}"))
}

fn render_pdf_page(path: &str, page: u32) -> Result<std::path::PathBuf, String> {
    use std::hash::{DefaultHasher, Hash, Hasher};

    if !std::path::Path::new(path).exists() {
        return Err(format!("PDF no longer exists: {path}"));
    }
    let cache = std::env::temp_dir().join("omarag-previews");
    std::fs::create_dir_all(&cache)
        .map_err(|error| format!("Could not create preview cache: {error}"))?;
    let mut hasher = DefaultHasher::new();
    path.hash(&mut hasher);
    page.hash(&mut hasher);
    if let Ok(modified) = std::fs::metadata(path).and_then(|metadata| metadata.modified()) {
        modified.hash(&mut hasher);
    }
    let stem = cache.join(format!("{:016x}-p{page}", hasher.finish()));
    let image = stem.with_extension("png");
    if !image.exists() {
        let status = Command::new("pdftoppm")
            .args([
                "-f",
                &page.to_string(),
                "-l",
                &page.to_string(),
                "-png",
                "-singlefile",
                "-scale-to",
                "1600",
            ])
            .arg(path)
            .arg(&stem)
            .status()
            .map_err(|error| format!("Could not render PDF page: {error}"))?;
        if !status.success() {
            return Err("PDF preview rendering failed.".into());
        }
    }
    prune_preview_cache(&cache, 300 * 1024 * 1024, &image);
    Ok(image)
}

fn render_pdf_page_with_anchors(
    path: &str,
    page: u32,
    primary: &[omarag_domain::CitationAnchor],
    context: &[omarag_domain::CitationAnchor],
) -> Result<std::path::PathBuf, String> {
    use std::hash::{DefaultHasher, Hash, Hasher};

    let base = render_pdf_page(path, page)?;
    let mut hasher = DefaultHasher::new();
    base.hash(&mut hasher);
    for anchor in primary.iter().chain(context) {
        anchor.page.hash(&mut hasher);
        anchor.doc_item_ref.hash(&mut hasher);
        anchor.x0.to_bits().hash(&mut hasher);
        anchor.y0.to_bits().hash(&mut hasher);
        anchor.x1.to_bits().hash(&mut hasher);
        anchor.y1.to_bits().hash(&mut hasher);
    }
    let highlighted = base.with_file_name(format!(
        "{}-h{:016x}.png",
        base.file_stem()
            .and_then(|value| value.to_str())
            .unwrap_or("page"),
        hasher.finish()
    ));
    if highlighted.exists() || (primary.is_empty() && context.is_empty()) {
        return Ok(if highlighted.exists() {
            highlighted
        } else {
            base
        });
    }
    let mut image = image::open(&base)
        .map_err(|error| format!("Could not read PDF preview: {error}"))?
        .to_rgba8();
    let (width, height) = image.dimensions();
    let mut paint = |anchor: &omarag_domain::CitationAnchor, strong: bool| {
        if anchor.page != page || width == 0 || height == 0 {
            return;
        }
        let x0 = (anchor.x0.clamp(0.0, 1.0) * f64::from(width)).floor() as u32;
        let y0 = (anchor.y0.clamp(0.0, 1.0) * f64::from(height)).floor() as u32;
        let x1 = (anchor.x1.clamp(0.0, 1.0) * f64::from(width)).ceil() as u32;
        let y1 = (anchor.y1.clamp(0.0, 1.0) * f64::from(height)).ceil() as u32;
        let x1 = x1.min(width.saturating_sub(1));
        let y1 = y1.min(height.saturating_sub(1));
        if x0 >= x1 || y0 >= y1 {
            return;
        }
        let fill = if strong {
            Rgba([255, 132, 70, 58])
        } else {
            Rgba([255, 220, 80, 34])
        };
        let outline = if strong {
            Rgba([240, 82, 110, 220])
        } else {
            Rgba([230, 170, 40, 150])
        };
        for y in y0..=y1 {
            for x in x0..=x1 {
                image.get_pixel_mut(x, y).blend(&fill);
            }
        }
        for inset in 0..2_u32 {
            let left = x0.saturating_add(inset).min(x1);
            let right = x1.saturating_sub(inset).max(left);
            let top = y0.saturating_add(inset).min(y1);
            let bottom = y1.saturating_sub(inset).max(top);
            for x in left..=right {
                image.put_pixel(x, top, outline);
                image.put_pixel(x, bottom, outline);
            }
            for y in top..=bottom {
                image.put_pixel(left, y, outline);
                image.put_pixel(right, y, outline);
            }
        }
    };
    for anchor in context {
        paint(anchor, false);
    }
    for anchor in primary {
        paint(anchor, true);
    }
    image
        .save(&highlighted)
        .map_err(|error| format!("Could not cache highlighted preview: {error}"))?;
    Ok(highlighted)
}

fn prune_preview_cache(cache: &std::path::Path, limit: u64, keep: &std::path::Path) {
    let Ok(entries) = std::fs::read_dir(cache) else {
        return;
    };
    let mut files = entries
        .flatten()
        .filter_map(|entry| {
            let metadata = entry.metadata().ok()?;
            metadata
                .is_file()
                .then(|| (entry.path(), metadata.len(), metadata.modified().ok()))
        })
        .collect::<Vec<_>>();
    let mut total = files.iter().map(|(_, size, _)| size).sum::<u64>();
    files.sort_by_key(|(_, _, modified)| *modified);
    for (path, size, _) in files {
        if total <= limit {
            break;
        }
        if path != keep && std::fs::remove_file(&path).is_ok() {
            total = total.saturating_sub(size);
        }
    }
}

fn citation_source_path(state: &AppState, citation: &omarag_domain::Citation) -> Option<String> {
    if let Some(source) = citation.source_uri.as_deref() {
        if let Ok(uri) = Url::parse(source)
            && uri.scheme() == "file"
            && let Ok(path) = uri.to_file_path()
        {
            return Some(path.to_string_lossy().into_owned());
        }
        if !source.contains("://") {
            return Some(source.to_owned());
        }
    }
    citation
        .document_id
        .as_ref()
        .and_then(|id| state.documents.iter().find(|document| &document.id == id))
        .or_else(|| {
            citation.document_title.as_ref().and_then(|title| {
                state
                    .documents
                    .iter()
                    .find(|document| &document.title == title)
            })
        })
        .map(|document| document.source.clone())
}

fn citation_preview_targets(
    state: &AppState,
    visual: &VisualInspectorState,
) -> Vec<CitationPreviewTarget> {
    related_page_refs(state, Some(&visual.evidence))
        .into_iter()
        .filter_map(|(citation_index, page_index, page)| {
            let citation = state.chat.citations.get(citation_index)?;
            let path = citation_source_path(state, citation)?;
            let source_title = citation.document_title.as_deref().unwrap_or("Source");
            let title = format!("Page · {source_title} · p.{page}");
            Some(CitationPreviewTarget {
                citation_index,
                page_index,
                path,
                page,
                remote_preview: page_index == 0
                    || (citation.primary_anchors.is_empty() && citation.context_anchors.is_empty()),
                title,
                primary_anchors: citation
                    .primary_anchors
                    .iter()
                    .filter(|anchor| anchor.page == page)
                    .cloned()
                    .collect(),
                context_anchors: citation
                    .context_anchors
                    .iter()
                    .filter(|anchor| anchor.page == page)
                    .cloned()
                    .collect(),
            })
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
fn schedule_chat_previews(
    state: &AppState,
    visual: &VisualInspectorState,
    enabled: bool,
    client: Arc<HttpOmaRagClient>,
    picker: &Picker,
    previews: &mut Vec<ChatImagePreview>,
    pending: &mut BTreeSet<(String, u32)>,
    scope: &mut PreviewScope,
    tx: mpsc::Sender<BackendMessage>,
) {
    if !enabled {
        scope.token.cancel();
        scope.keys.clear();
        pending.clear();
        return;
    }
    let targets = citation_preview_targets(state, visual);
    let keys = targets
        .iter()
        .map(|target| (target.path.clone(), target.page))
        .collect::<Vec<_>>();
    if scope.keys != keys {
        scope.token.cancel();
        scope.token = CancellationToken::new();
        scope.keys = keys;
        pending.clear();
    }
    previews.retain(|preview| {
        targets
            .iter()
            .any(|target| target.path == preview.pdf_path && target.page == preview.page)
    });
    pending.retain(|key| {
        targets
            .iter()
            .any(|target| target.path == key.0 && target.page == key.1)
    });
    for target in targets {
        let CitationPreviewTarget {
            citation_index,
            page_index,
            path,
            page,
            remote_preview,
            title,
            primary_anchors,
            context_anchors,
        } = target;
        let key = (path.clone(), page);
        if previews
            .iter()
            .any(|preview| preview.pdf_path == path && preview.page == page)
            || !pending.insert(key.clone())
        {
            continue;
        }
        let picker = picker.clone();
        let tx = tx.clone();
        let cancellation = scope.token.clone();
        let workspace = state.active_workspace.clone();
        let run_id = state.chat.last_run.clone();
        let client = Arc::clone(&client);
        tokio::spawn(async move {
            let remote =
                if remote_preview && let (Some(workspace), Some(run_id)) = (workspace, run_id) {
                    client
                        .citation_preview(workspace, run_id, citation_index, 1400)
                        .await
                        .ok()
                } else {
                    None
                };
            let render = tokio::task::spawn_blocking(move || {
                let image = if let Some(bytes) = remote {
                    ImageReader::new(Cursor::new(bytes))
                        .with_guessed_format()
                        .map_err(|error| error.to_string())?
                        .decode()
                        .map_err(|error| error.to_string())?
                } else {
                    let image_path = render_pdf_page_with_anchors(
                        &path,
                        page,
                        &primary_anchors,
                        &context_anchors,
                    )?;
                    ImageReader::open(image_path)
                        .map_err(|error| error.to_string())?
                        .decode()
                        .map_err(|error| error.to_string())?
                };
                Ok(ChatImagePreview::new(
                    citation_index,
                    page_index,
                    path,
                    page,
                    title,
                    picker.new_resize_protocol(image),
                ))
            });
            let result = tokio::select! {
                _ = cancellation.cancelled() => return,
                result = render => result
                    .map_err(|error| error.to_string())
                    .and_then(|result| result),
            };
            if cancellation.is_cancelled() {
                return;
            }
            let _ = tx.send(BackendMessage::PreviewLoaded { key, result }).await;
        });
    }
}

#[allow(clippy::too_many_arguments)]
fn schedule_media_previews(
    visual: &VisualInspectorState,
    enabled: bool,
    client: Arc<HttpOmaRagClient>,
    picker: &Picker,
    previews: &mut Vec<MediaImagePreview>,
    pending: &mut BTreeSet<String>,
    scope: &mut MediaPreviewScope,
    tx: mpsc::Sender<BackendMessage>,
) {
    if !enabled {
        scope.token.cancel();
        scope.keys.clear();
        pending.clear();
        return;
    }
    let targets = visual
        .evidence
        .media
        .iter()
        .filter(|asset| asset.is_individual_asset())
        .take(VisualEvidenceResponse::MAX_MEDIA)
        .filter_map(|asset| {
            let url = asset.image_url()?.to_owned();
            let media_id = asset.media_id.clone();
            let key = format!("{media_id}\u{1f}{url}");
            Some((key, media_id, url))
        })
        .collect::<Vec<_>>();
    let keys = targets
        .iter()
        .map(|(key, _, _)| key.clone())
        .collect::<Vec<_>>();
    if scope.keys != keys {
        scope.token.cancel();
        scope.token = CancellationToken::new();
        scope.keys = keys;
        pending.clear();
    }
    let media_ids = targets
        .iter()
        .map(|(_, media_id, _)| media_id.as_str())
        .collect::<BTreeSet<_>>();
    previews.retain(|preview| media_ids.contains(preview.media_id.as_str()));
    pending.retain(|key| targets.iter().any(|(target, _, _)| target == key));
    for (key, media_id, url) in targets {
        if previews.iter().any(|preview| preview.media_id == media_id)
            || !pending.insert(key.clone())
        {
            continue;
        }
        let picker = picker.clone();
        let tx = tx.clone();
        let cancellation = scope.token.clone();
        let client = Arc::clone(&client);
        tokio::spawn(async move {
            let result = async {
                let bytes = client
                    .visual_evidence_asset(url)
                    .await
                    .map_err(|error| error.to_string())?;
                tokio::task::spawn_blocking(move || {
                    let image = ImageReader::new(Cursor::new(bytes))
                        .with_guessed_format()
                        .map_err(|error| error.to_string())?
                        .decode()
                        .map_err(|error| error.to_string())?;
                    Ok(MediaImagePreview::new(
                        media_id,
                        picker.new_resize_protocol(image),
                    ))
                })
                .await
                .map_err(|error| error.to_string())?
            };
            let result = tokio::select! {
                _ = cancellation.cancelled() => return,
                result = result => result,
            };
            if !cancellation.is_cancelled() {
                let _ = tx
                    .send(BackendMessage::MediaPreviewLoaded { key, result })
                    .await;
            }
        });
    }
}

fn citation_preview_position(
    state: &AppState,
    visual: &VisualInspectorState,
    path: &str,
    page: u32,
) -> usize {
    citation_preview_targets(state, visual)
        .iter()
        .position(|target| target.path == path && target.page == page)
        .unwrap_or(usize::MAX)
}

fn analyze_import(selected: &[String], existing: &[String]) -> ImportPreflight {
    let pdfs = expand_import_paths(selected);
    let existing = existing
        .iter()
        .filter_map(|path| std::fs::canonicalize(path).ok())
        .collect::<BTreeSet<_>>();
    let mut report = ImportPreflight {
        selected: selected.to_vec(),
        pdfs: pdfs.clone(),
        ..ImportPreflight::default()
    };
    if pdfs.is_empty() {
        report.error = Some("The selection contains no PDF files.".into());
    }
    for path in &pdfs {
        let file_path = std::path::Path::new(path);
        if std::fs::canonicalize(file_path)
            .ok()
            .is_some_and(|path| existing.contains(&path))
        {
            report.duplicates.push(path.clone());
        }
        let mut header = [0_u8; 5];
        let readable_pdf = std::fs::File::open(file_path)
            .and_then(|mut file| file.read_exact(&mut header))
            .is_ok()
            && &header == b"%PDF-";
        if !readable_pdf {
            report.unreadable.push(path.clone());
            continue;
        }
        report.total_bytes = report
            .total_bytes
            .saturating_add(file_path.metadata().map_or(0, |metadata| metadata.len()));
        if pdf_info(path).is_some_and(|(encrypted, _)| encrypted) {
            report.encrypted.push(path.clone());
        }
    }
    report.estimated_index_bytes = report.total_bytes.saturating_mul(3) / 2;
    report.estimated_seconds = (report.total_bytes / (4 * 1024 * 1024))
        .max(pdfs.len() as u64 * 2)
        .max(1);
    report.busy = false;
    report
}

fn pdf_info(path: &str) -> Option<(bool, u32)> {
    let output = Command::new("pdfinfo").arg(path).output().ok()?;
    let text = String::from_utf8_lossy(&output.stdout);
    let encrypted = text
        .lines()
        .find_map(|line| line.strip_prefix("Encrypted:"))
        .is_some_and(|value| value.trim().starts_with("yes"));
    let pages = text
        .lines()
        .find_map(|line| line.strip_prefix("Pages:"))
        .and_then(|value| value.trim().parse().ok())
        .unwrap_or(0);
    Some((encrypted, pages))
}

/// Base64 for OSC 52. Hand-rolled to keep a fallback path from pulling in a
/// dependency; the input is a clipboard selection, so it is never large.
fn base64_encode(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for group in bytes.chunks(3) {
        let b = [
            group[0],
            group.get(1).copied().unwrap_or(0),
            group.get(2).copied().unwrap_or(0),
        ];
        let packed = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        for index in 0..4 {
            if index <= group.len() {
                let shift = 18 - index * 6;
                out.push(char::from(ALPHABET[((packed >> shift) & 0x3F) as usize]));
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// Ask the terminal itself to hold the text. This is the only route that works
/// over SSH, and the only one left when neither helper is installed. Terminals
/// may refuse it, which we cannot detect — hence last, never first.
fn copy_via_terminal(value: &str) -> Result<(), String> {
    let mut out = std::io::stdout();
    write!(out, "\x1b]52;c;{}\x07", base64_encode(value.as_bytes()))
        .map_err(|error| format!("No clipboard helper available: {error}"))?;
    out.flush()
        .map_err(|error| format!("No clipboard helper available: {error}"))
}

fn copy_text(value: &str) -> Result<(), String> {
    let spawned = Command::new("wl-copy")
        .stdin(Stdio::piped())
        .spawn()
        .or_else(|_| {
            Command::new("xclip")
                .args(["-selection", "clipboard"])
                .stdin(Stdio::piped())
                .spawn()
        });
    let mut child = match spawned {
        Ok(child) => child,
        Err(_) => return copy_via_terminal(value),
    };
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| "Clipboard input unavailable".to_string())?;
    stdin
        .write_all(value.as_bytes())
        .map_err(|error| error.to_string())?;
    drop(stdin);
    let status = child.wait().map_err(|error| error.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("Clipboard helper exited with {status}"))
    }
}

fn export_chat(workspace: &str, session: &omarag_app::ChatSession) -> Result<(), String> {
    let directory = omarag_data_dir().join("exports");
    std::fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    let safe = workspace
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() {
                character
            } else {
                '-'
            }
        })
        .collect::<String>();
    let path = directory.join(format!(
        "{}-chat-{}.md",
        safe.trim_matches('-'),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |duration| duration.as_secs())
    ));
    let mut content = format!(
        "# {workspace}\n\n**Question:** {}\n\n{}\n\n## Sources\n",
        session.question, session.answer
    );
    for (index, citation) in session.citations.iter().enumerate() {
        let pages = citation
            .pages
            .iter()
            .map(u32::to_string)
            .collect::<Vec<_>>()
            .join(", ");
        content.push_str(&format!(
            "\n{}. {} — p. {}\n",
            index + 1,
            citation.document_title.as_deref().unwrap_or("Source"),
            pages
        ));
    }
    std::fs::write(&path, content).map_err(|error| error.to_string())?;
    open_pdf_or_path(&path)
}

fn open_pdf_or_path(path: &std::path::Path) -> Result<(), String> {
    Command::new("xdg-open")
        .arg(path)
        .spawn()
        .map(|_| ())
        .map_err(|error| error.to_string())
}

fn omarag_config_dir() -> std::path::PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|home| std::path::PathBuf::from(home).join(".config"))
        })
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("omarag")
}

fn omarag_data_dir() -> std::path::PathBuf {
    std::env::var_os("XDG_DATA_HOME")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|home| std::path::PathBuf::from(home).join(".local/share"))
        })
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("omarag")
}

fn load_preferences(path: &std::path::Path) -> Result<UiPreferences, String> {
    let content = std::fs::read_to_string(path).map_err(|error| error.to_string())?;
    serde_json::from_str(&content).map_err(|error| error.to_string())
}

fn save_preferences(path: &std::path::Path, encoded: &[u8]) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, encoded).map_err(|error| error.to_string())?;
    std::fs::rename(&temporary, path).map_err(|error| error.to_string())
}

fn remember_chat_session(state: &mut AppState, timestamp: String) {
    let Some(workspace) = state.active_workspace.clone() else {
        return;
    };
    if state.chat.submitted_question.trim().is_empty() || state.chat.answer.trim().is_empty() {
        return;
    }
    let session = omarag_app::ChatSession {
        workspace_id: workspace.clone(),
        session_id: state.chat.receipt.as_ref().map_or_else(
            || {
                state
                    .conversation_ids
                    .get(&workspace)
                    .cloned()
                    .unwrap_or_default()
            },
            |receipt| receipt.session_id.clone(),
        ),
        question: state.chat.submitted_question.clone(),
        answer: state.chat.answer.clone(),
        citations: state.chat.citations.clone(),
        receipt: state.chat.receipt.clone(),
        scope_document_id: state.chat.scope_document_id.clone(),
        scope_title: state.chat.scope_title.clone(),
        created_at: timestamp,
    };
    let sessions = state.chat_sessions.entry(workspace).or_default();
    sessions.retain(|item| item.question != session.question || item.answer != session.answer);
    sessions.insert(0, session);
    sessions.truncate(50);
    state.history_cursor = 0;
}

fn apply_backend_message(state: &mut AppState, message: BackendMessage) -> bool {
    match message {
        BackendMessage::WarmupFinished => {}
        BackendMessage::WorkspaceOpened(result) => match result {
            Ok(id) => {
                update(state, Action::WorkspaceOpened(id));
                return true;
            }
            Err(error) => {
                update(state, Action::OperationFinished);
                notify_error(state, error);
            }
        },
        BackendMessage::WorkspaceCreated(result) => match result {
            Ok(workspace) => {
                let id = workspace.id.clone();
                state.assign_profile_at(id.clone(), state.profile_cursor);
                state.creating_workspace = false;
                state.workspaces.push(WorkspaceSummary {
                    id: workspace.id,
                    name: workspace.name,
                    path: workspace.path,
                    read_only: workspace.read_only,
                    updated_at: workspace.updated_at,
                    etag: workspace.etag,
                });
                state.workspace_cursor = state.workspaces.len().saturating_sub(1);
                update(state, Action::WorkspaceOpened(id));
                // The user only opened this dialog because they wanted to add
                // books; carry them straight on to the file browser.
                if std::mem::take(&mut state.import_after_library) {
                    omarag_tui::input::open_import_browser(state);
                }
                update(
                    state,
                    Action::Notify(Notification {
                        level: NotificationLevel::Info,
                        message: "Library created.".into(),
                    }),
                );
                return true;
            }
            Err(error) => {
                update(state, Action::OperationFinished);
                notify_error(state, error);
            }
        },
        BackendMessage::LibraryDeleted {
            id,
            physical,
            result,
        } => match result {
            Ok(()) => {
                let was_active = state.active_workspace.as_ref() == Some(&id);
                state.workspaces.retain(|library| library.id != id);
                state.workspace_profiles.remove(&id);
                state.workspace_custom_profiles.remove(&id);
                state.chat_sessions.remove(&id);
                state.conversation_ids.remove(&id);
                state.workspace_cursor = state
                    .workspace_cursor
                    .min(state.workspaces.len().saturating_sub(1));
                if was_active {
                    state.active_workspace = None;
                    state.documents.clear();
                    state.sources.clear();
                    state.jobs.retain(|_, job| job.workspace_id != id);
                    state.chat = Default::default();
                }
                update(state, Action::OperationFinished);
                update(
                    state,
                    Action::Notify(Notification {
                        level: NotificationLevel::Info,
                        message: if physical {
                            "Library and its local files were deleted.".into()
                        } else {
                            "Library removed. Its files remain on disk.".into()
                        },
                    }),
                );
                if was_active {
                    return true;
                }
            }
            Err(error) => {
                update(state, Action::OperationFinished);
                notify_error(state, error);
            }
        },
        BackendMessage::ExternalOpened(result) => {
            if let Err(error) = result {
                notify_error(state, error);
            }
        }
        BackendMessage::ClipboardCopied {
            selection,
            characters,
            result,
        } => match result {
            Ok(()) => {
                // Say how much landed in the clipboard. A selection made by
                // dragging has no other confirmation that it caught what the
                // eye thought it caught.
                let what = if selection { "Selection" } else { "Text" };
                let unit = if characters == 1 {
                    "character"
                } else {
                    "characters"
                };
                update(
                    state,
                    Action::Notify(Notification {
                        level: NotificationLevel::Info,
                        message: format!("{what} copied — {characters} {unit}."),
                    }),
                );
            }
            Err(error) => notify_error(state, error),
        },
        BackendMessage::ImportAnalyzed(preflight) => {
            state.library.preflight = preflight;
        }
        BackendMessage::DocumentDeleted(result) => match result {
            Ok(document) => {
                state.documents.retain(|item| item.id != document.id);
                state.undo = Some(UndoAction::RemovedDocument(Box::new(document)));
                state.asset_cursor = state.asset_cursor.saturating_sub(1);
                update(state, Action::OperationFinished);
                update(
                    state,
                    Action::Notify(Notification {
                        level: NotificationLevel::Info,
                        message: "Removed from index. Original PDF kept. Ctrl+Z restores it."
                            .into(),
                    }),
                );
            }
            Err(error) => {
                update(state, Action::OperationFinished);
                notify_error(state, error);
            }
        },
        BackendMessage::DocumentRestored(result) => match result {
            Ok(_) => {
                update(state, Action::OperationFinished);
                update(
                    state,
                    Action::Notify(Notification {
                        level: NotificationLevel::Info,
                        message: "Restored from the original PDF.".into(),
                    }),
                );
                return true;
            }
            Err(error) => notify_error(state, error),
        },
        BackendMessage::PreviewLoaded { .. }
        | BackendMessage::MediaPreviewLoaded { .. }
        | BackendMessage::VisualEvidenceLoaded { .. }
        | BackendMessage::HardwareProfileLoaded(_)
        | BackendMessage::HardwareRecommendationLoaded { .. } => {
            unreachable!("runtime-only messages are handled in the event loop")
        }
        BackendMessage::RunStarted(result) => match result {
            Ok(id) => {
                update(state, Action::RunStarted(id));
            }
            Err(error) => {
                update(state, Action::RunFailed(error.clone()));
                notify_error(state, error);
            }
        },
        BackendMessage::RunCancelled(result) => match result {
            Ok(()) => {
                update(state, Action::RunCancelled);
            }
            Err(error) => {
                update(state, Action::OperationFinished);
                notify_error(state, error);
            }
        },
        BackendMessage::SearchCompleted(result) => match result {
            Ok(explanation) => {
                let duration = explanation.timing.total_ms;
                let count = explanation.ranked.len();
                update(state, Action::SearchCompleted(explanation.ranked.clone()));
                state.search.explanation = Some(explanation);
                update(
                    state,
                    Action::Notify(Notification {
                        level: NotificationLevel::Info,
                        message: format!("Retrieval inspector · {count} hits · {duration:.1} ms"),
                    }),
                );
            }
            Err(error) => {
                update(state, Action::SearchFailed(error.clone()));
                notify_error(state, error);
            }
        },
        BackendMessage::ImportAccepted(result) => match result {
            Ok(id) => {
                let directory = state.file_browser.current_dir.clone();
                state.file_browser.history.retain(|item| item != &directory);
                state.file_browser.history.insert(0, directory);
                state.file_browser.history.truncate(12);
                update(state, Action::ImportAccepted(id));
                update(
                    state,
                    Action::Notify(Notification {
                        level: NotificationLevel::Info,
                        message: "Import queued in the background.".into(),
                    }),
                );
            }
            Err(error) => {
                update(state, Action::ImportFailed(error.clone()));
                notify_error(state, error);
            }
        },
        BackendMessage::JobUpdated(result) => match result {
            Ok(_) => {
                update(state, Action::OperationFinished);
            }
            Err(error) => {
                update(state, Action::OperationFinished);
                notify_error(state, error);
            }
        },
        BackendMessage::JobsLoaded(result) => {
            if let Ok(jobs) = result {
                let workspace = state.active_workspace.as_ref();
                update(
                    state,
                    Action::JobsLoaded(
                        jobs.into_iter()
                            .filter(|job| workspace.is_none_or(|id| &job.workspace_id == id))
                            .collect(),
                    ),
                );
            }
        }
        BackendMessage::WorkspaceFeaturesLoaded(result) => match result {
            Ok(features) => apply_workspace_features(state, *features),
            Err(error) => notify_error(state, error),
        },
        BackendMessage::BackupCreated(result) => match result {
            Ok(backup) => {
                state.backups.insert(0, backup);
                update(state, Action::OperationFinished);
                update(
                    state,
                    Action::Notify(Notification {
                        level: NotificationLevel::Info,
                        message: "Backup created and verified.".into(),
                    }),
                );
            }
            Err(error) => {
                update(state, Action::OperationFinished);
                notify_error(state, error);
            }
        },
        BackendMessage::SourceCreated(result) => match result {
            Ok(source) => {
                update(state, Action::SourceCreated(source));
                update(
                    state,
                    Action::Notify(Notification {
                        level: NotificationLevel::Info,
                        message: "Source saved.".into(),
                    }),
                );
            }
            Err(error) => {
                update(state, Action::OperationFinished);
                notify_error(state, error);
            }
        },
        BackendMessage::ConfigSaved(result) => match result {
            Ok(config) => {
                update(state, Action::ConfigSaved(config));
                update(
                    state,
                    Action::Notify(Notification {
                        level: NotificationLevel::Info,
                        message: "Configuration saved atomically.".into(),
                    }),
                );
            }
            Err(error) => {
                update(state, Action::OperationFinished);
                notify_error(state, error);
            }
        },
        BackendMessage::AutomaticStackPreflight { workspace, result } => {
            state.model_manager.busy = false;
            if state.active_workspace.as_ref() != Some(&workspace) {
                state.automatic_stack_preflight = None;
                state.model_manager.transfer_status = "Automatic stack preview expired".into();
                return false;
            }
            match result {
                Ok(preflight) => {
                    state.model_manager.transfer_status = if preflight.requires_reindex {
                        "Automatic stack requires a full rebuild".into()
                    } else {
                        "Automatic stack ready for review".into()
                    };
                    state.automatic_stack_preflight = Some(preflight);
                    state.overlay = Some(Overlay::AutomaticStackPreflight);
                    state.input_mode = InputMode::Nav;
                }
                Err(error) => {
                    state.automatic_stack_preflight = None;
                    state.model_manager.transfer_status = "Automatic stack unavailable".into();
                    notify_error(state, error);
                }
            }
        }
        BackendMessage::AutomaticStackApplied(result) => {
            state.model_manager.busy = false;
            state.automatic_stack_preflight = None;
            match result {
                Ok(config) => {
                    update(state, Action::ConfigSaved(config));
                    state.model_manager.transfer_status = "Automatic stack applied".into();
                    update(
                        state,
                        Action::Notify(Notification {
                            level: NotificationLevel::Info,
                            message: "Automatic model stack applied.".into(),
                        }),
                    );
                }
                Err(error) => {
                    state.model_manager.transfer_status = "Automatic stack apply failed".into();
                    notify_error(state, error);
                }
            }
        }
        BackendMessage::ModelCatalogLoaded(result) => {
            state.model_manager.busy = false;
            match result {
                Ok(mut catalog) => {
                    let query = state.model_manager.query.value.trim();
                    if !query.is_empty() {
                        catalog.entries.sort_by(|left, right| {
                            fuzzy_score(&right.id, query)
                                .unwrap_or_default()
                                .cmp(&fuzzy_score(&left.id, query).unwrap_or_default())
                        });
                    }
                    state.model_manager.entries = catalog.entries;
                    state.model_manager.packages = catalog.packages;
                    state.model_manager.scanned = catalog.scanned;
                    state.model_manager.compatible = catalog.compatible;
                    state.model_manager.truncated = catalog.truncated;
                    state.model_manager.cursor = state
                        .model_manager
                        .cursor
                        .min(state.model_manager.entries.len().saturating_sub(1));
                    state.model_manager.package_cursor = state
                        .model_manager
                        .package_cursor
                        .min(state.model_manager.packages.len().saturating_sub(1));
                    state.model_manager.transfer_status = format!(
                        "{} compatible · {} scanned · {} / {}",
                        state.model_manager.entries.len(),
                        state.model_manager.scanned,
                        state.model_manager.source.label(),
                        state.model_manager.category.label(),
                    );
                }
                Err(error) => {
                    state.model_manager.transfer_status = "Catalog unavailable".into();
                    notify_error(state, error);
                }
            }
        }
        BackendMessage::ModelTransfer(transfer) => {
            state.model_manager.busy = true;
            state.model_manager.transfer_status =
                format!("{} · {}", transfer.model, transfer.status);
            state.model_manager.transfer_completed = transfer.completed;
            state.model_manager.transfer_total = transfer.total;
        }
        BackendMessage::ModelOperationFinished {
            model,
            operation,
            result,
        } => {
            state.model_manager.busy = false;
            state.model_manager.transfer_completed = 0;
            state.model_manager.transfer_total = 0;
            match result {
                Ok(()) => {
                    let verb = match operation {
                        ModelOperation::Download => "Downloaded",
                        ModelOperation::Load => "Loaded",
                        ModelOperation::Unload => "Unloaded",
                        ModelOperation::Delete => "Deleted",
                    };
                    state.model_manager.transfer_status = format!("{verb} {model}");
                    if matches!(operation, ModelOperation::Download) {
                        for entry in &mut state.model_manager.entries {
                            if model.contains(&entry.id) {
                                entry.installed = true;
                            }
                        }
                        for package in &mut state.model_manager.packages {
                            for package_model in &mut package.models {
                                if model == package_model.download_name {
                                    package_model.installed = true;
                                }
                            }
                        }
                    }
                    if matches!(operation, ModelOperation::Delete) {
                        state
                            .model_manager
                            .entries
                            .retain(|entry| entry.id != model);
                        state.model_manager.cursor = state
                            .model_manager
                            .cursor
                            .min(state.model_manager.entries.len().saturating_sub(1));
                        for package in &mut state.model_manager.packages {
                            for package_model in &mut package.models {
                                if package_model.model == model
                                    || package_model.download_name == model
                                {
                                    package_model.installed = false;
                                }
                            }
                        }
                    }
                    update(
                        state,
                        Action::Notify(Notification {
                            level: NotificationLevel::Info,
                            message: format!("{verb} {model}."),
                        }),
                    );
                }
                Err(error) => {
                    state.model_manager.transfer_status = "Model operation failed".into();
                    notify_error(state, error);
                }
            }
        }
        BackendMessage::ModelPackageFinished {
            name,
            installed_models,
            activation_status,
            result,
        } => {
            state.model_manager.busy = false;
            state.model_manager.transfer_completed = 0;
            state.model_manager.transfer_total = 0;
            for package in &mut state.model_manager.packages {
                for package_model in &mut package.models {
                    if installed_models
                        .iter()
                        .any(|installed| installed == &package_model.download_name)
                    {
                        package_model.installed = true;
                    }
                }
            }
            match result {
                Ok(config) => {
                    if let Some(config) = config {
                        update(state, Action::ConfigSaved(config));
                    }
                    state.model_manager.transfer_status = format!("{name}: {activation_status}");
                    update(
                        state,
                        Action::Notify(Notification {
                            level: if activation_status.contains("requires") {
                                NotificationLevel::Warning
                            } else {
                                NotificationLevel::Info
                            },
                            message: format!("Package {name} {activation_status}."),
                        }),
                    );
                }
                Err(error) => {
                    let error = if error.contains("404 Not Found") {
                        format!(
                            "{error} · The backend does not provide the package installer yet; restart the OmaRag daemon."
                        )
                    } else {
                        error
                    };
                    state.model_manager.transfer_status =
                        format!("Package {name} failed · {error}");
                    notify_error(state, error);
                }
            }
        }
        BackendMessage::FilesystemChanged => {}
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn linux_gpu_probe_does_not_require_external_tools() {
        let gpu = gpu_metrics();
        if gpu.vram_total > 0 {
            assert!(gpu.vram_used <= gpu.vram_total);
            assert!(gpu.name.is_some());
        }
    }

    #[test]
    fn import_preflight_expands_pdfs_and_detects_duplicates() {
        let root = std::env::temp_dir().join(format!("oracle-preflight-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let pdf = root.join("spec.pdf");
        std::fs::write(&pdf, b"%PDF-1.7\n").unwrap();
        let report = analyze_import(
            &[root.to_string_lossy().into_owned()],
            &[pdf.to_string_lossy().into_owned()],
        );
        assert_eq!(report.pdfs.len(), 1);
        assert_eq!(report.duplicates.len(), 1);
        assert!(report.total_bytes > 0);
        assert!(!report.busy);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn preferences_round_trip_without_serializing_runtime_state() {
        let mut state = AppState {
            theme_index: 2,
            view: omarag_app::View::Books,
            focus_pane: omarag_app::FocusPane::Inspector,
            ..AppState::default()
        };
        state.file_browser.current_dir = "/tmp/docs".into();
        state.library.filter = omarag_app::LibraryFilter::Duplicates;
        let encoded = serde_json::to_string(&state.preferences()).unwrap();
        let mut restored = AppState::default();
        restored.apply_preferences(serde_json::from_str(&encoded).unwrap());
        assert_eq!(restored.theme_index, 2);
        assert_eq!(restored.view, omarag_app::View::Books);
        assert_eq!(restored.focus_pane, omarag_app::FocusPane::Workspace);
        assert_eq!(restored.focus, omarag_app::FocusPanel::Sources);
        assert_eq!(
            restored.library.filter,
            omarag_app::LibraryFilter::Duplicates
        );
    }

    #[test]
    fn creating_a_library_closes_the_dialog_and_activates_it() {
        // The whole point of the flow: after the backend confirms, the dialog
        // must go away and the new library must become active. If it does not,
        // the user presses Enter again and creates duplicates.
        let mut state = AppState {
            overlay: Some(omarag_app::Overlay::Workspaces),
            creating_workspace: true,
            ..AppState::default()
        };
        state.workspace_name.set("Bautechnik".to_owned());

        let command = omarag_tui::input::handle_event(
            &mut state,
            crossterm::event::Event::Key(crossterm::event::KeyEvent::new(
                crossterm::event::KeyCode::Enter,
                crossterm::event::KeyModifiers::NONE,
            )),
        );
        assert!(
            matches!(command, Some(UiCommand::CreateWorkspace(_))),
            "Enter must ask the backend to create the library"
        );

        apply_backend_message(
            &mut state,
            BackendMessage::WorkspaceCreated(Ok(WorkspaceManifest {
                schema_version: 1,
                id: "ws-bautechnik-1".into(),
                name: "Bautechnik".into(),
                created_at: "2026-08-18T06:00:00Z".into(),
                updated_at: "2026-08-18T06:00:00Z".into(),
                path: "/tmp/ws".into(),
                read_only: false,
                haiku_compatible_range: "latest-gated".into(),
                haiku_update_policy: "latest-gated".into(),
                haiku_last_verified: None,
                database_schema_version: "detected".into(),
                embedding_provider: "ollama".into(),
                embedding_model: "qwen3-embedding:0.6b".into(),
                vector_dimension: Some(1024),
                processing_profile: "default".into(),
                evidence_mode: omarag_domain::EvidenceMode::default(),
                document_policy: "prefer-current".into(),
                privacy_mode: omarag_domain::PrivacyMode::DeviceOnly,
                cloud_acknowledged: false,
                etag: "etag".into(),
            })),
        );

        assert_eq!(state.overlay, None, "dialog must close after creation");
        assert!(!state.creating_workspace, "creation mode must end");
        assert!(!state.operation.active, "spinner must stop");
        assert_eq!(state.active_workspace.as_deref(), Some("ws-bautechnik-1"));
        assert_eq!(state.workspaces.len(), 1);
    }

    #[test]
    fn copied_chat_selection_gets_a_specific_toast() {
        let mut state = AppState::default();

        apply_backend_message(
            &mut state,
            BackendMessage::ClipboardCopied {
                selection: true,
                characters: 12,
                result: Ok(()),
            },
        );

        assert_eq!(
            state.notifications.last().map(|item| item.message.as_str()),
            Some("Selection copied — 12 characters.")
        );
    }

    #[test]
    fn package_endpoint_mismatch_tells_the_user_to_restart_the_daemon() {
        let mut state = AppState::default();
        state.model_manager.busy = true;

        apply_backend_message(
            &mut state,
            BackendMessage::ModelPackageFinished {
                name: "Balanced".into(),
                installed_models: Vec::new(),
                activation_status: String::new(),
                result: Err("HTTP status client error (404 Not Found)".into()),
            },
        );

        assert!(!state.model_manager.busy);
        assert!(state.model_manager.transfer_status.contains("restart"));
        assert!(
            state
                .notifications
                .last()
                .is_some_and(|item| item.message.contains("restart the OmaRag daemon"))
        );
    }

    #[test]
    fn backend_scan_owns_tier_while_partial_fields_keep_safe_fallbacks() {
        let fallback = HardwareProfileResponse {
            tier: omarag_domain::HardwareTier::new(4).unwrap(),
            tier_label: "Legacy tier 4".into(),
            limiting_factor: "VRAM".into(),
            catalog_version: "bundled".into(),
            ..HardwareProfileResponse::default()
        };
        let backend = HardwareProfileResponse {
            tier: omarag_domain::HardwareTier::new(8).unwrap(),
            profile: PerformanceProfile::Fast,
            recommendations: vec![omarag_domain::ModelRecommendation {
                role: "chat".into(),
                model: "recommended-chat".into(),
                ..omarag_domain::ModelRecommendation::default()
            }],
            ..HardwareProfileResponse::default()
        };
        let merged = merge_hardware_profile(fallback, backend, PerformanceProfile::Fast);
        assert_eq!(merged.tier.level(), 8);
        assert_eq!(merged.catalog_version, "bundled");
        assert_eq!(merged.profile, PerformanceProfile::Fast);
        assert_eq!(merged.recommendations[0].model, "recommended-chat");
    }
}
