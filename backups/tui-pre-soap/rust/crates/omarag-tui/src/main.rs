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
    Action, AppState, DocumentInsight, HardwareProfile, ImportPreflight, ModelCatalogResponse,
    ModelCategory, ModelSource, Notification, NotificationLevel, Overlay, UiPreferences,
    UndoAction, update,
};
use omarag_client::{HttpOmaRagClient, OmaRagClient};
use omarag_domain::{
    BackupSummary, ConfigDocument, DocumentSummary, DomainEvent, EventSubscription, JobId,
    JobSnapshot, QualityReport, RunId, RunRequest, SearchHit, SourceDefinition, WorkspaceId,
    WorkspaceManifest, WorkspaceSummary,
};
use omarag_tui::{
    ChatImagePreview, LoadedModel, RuntimeMetrics, Theme,
    input::{
        JobCommand, UiCommand, expand_import_paths, fuzzy_score, handle_event, refresh_file_browser,
    },
    render_with_previews,
};
use ratatui_image::picker::Picker;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{Read, Write, stdout},
    process::{Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use sysinfo::System;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use url::Url;
use uuid::Uuid;

#[derive(Debug, Parser)]
#[command(
    version,
    about = "Oracle of Daedalus · Offline Retrieval-Augmented Command-Line Environment"
)]
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
    ImportAnalyzed(ImportPreflight),
    DocumentDeleted(Result<omarag_domain::DocumentSummary, String>),
    DocumentRestored(Result<omarag_domain::DocumentSummary, String>),
    PreviewLoaded {
        key: (String, u32),
        result: Result<ChatImagePreview, String>,
    },
    RunStarted(Result<RunId, String>),
    RunCancelled(Result<(), String>),
    SearchCompleted(Result<Vec<SearchHit>, String>),
    ImportAccepted(Result<JobId, String>),
    JobUpdated(Result<(), String>),
    JobsLoaded(Result<Vec<JobSnapshot>, String>),
    WorkspaceFeaturesLoaded(Result<Box<WorkspaceFeatures>, String>),
    BackupCreated(Result<BackupSummary, String>),
    SourceCreated(Result<SourceDefinition, String>),
    ConfigSaved(Result<ConfigDocument, String>),
    ModelCatalogLoaded(Result<ModelCatalogResponse, String>),
    ModelTransfer(ModelTransfer),
    ModelOperationFinished {
        model: String,
        operation: ModelOperation,
        result: Result<(), String>,
    },
    FilesystemChanged,
}

#[derive(Debug)]
struct PreviewScope {
    token: CancellationToken,
    keys: Vec<(String, u32)>,
}

#[derive(Debug, Clone)]
struct CitationPreviewTarget {
    path: String,
    page: u32,
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

#[tokio::main]
async fn main() -> Result<()> {
    // Oracle of Daedalus is an explicitly themed full-screen application. Some desktop
    // sessions export NO_COLOR globally; override that preference for this
    // process so theme selection and keyboard focus remain visible.
    force_color_output(true);
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .with_writer(std::io::stderr)
        .init();
    let args = Args::parse();
    let model_api = ModelApi::new(args.url.clone(), args.token.clone())?;
    let client = Arc::new(HttpOmaRagClient::new(args.url, args.token)?);
    let mut state = AppState::default();
    let preferences_path = oracle_config_dir().join("ui-state.json");
    if let Ok(preferences) = load_preferences(&preferences_path) {
        state.apply_preferences(preferences);
    }
    bootstrap(client.as_ref(), &mut state).await;

    let mut terminal = ratatui::try_init().context("Terminal initialization failed")?;
    let image_picker = Picker::from_query_stdio().unwrap_or_else(|_| Picker::halfblocks());
    execute!(
        stdout(),
        SetTitle("Oracle of Daedalus"),
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
    let mut redraw = tokio::time::interval(Duration::from_millis(100));
    let mut jobs_refresh = tokio::time::interval(Duration::from_secs(2));
    let mut monitor_refresh = tokio::time::interval(Duration::from_secs(1));
    let mut model_refresh = tokio::time::interval(Duration::from_secs(3));
    let mut preferences_refresh = tokio::time::interval(Duration::from_secs(1));
    let mut system = System::new_all();
    let mut metrics = runtime_metrics(&system, 0);
    let mut chat_previews = Vec::new();
    let mut preview_pending = BTreeSet::new();
    let mut preview_scope = PreviewScope::default();
    let mut saved_preferences = serde_json::to_vec(&state.preferences()).unwrap_or_default();
    metrics.loaded_models = load_ollama_models(&model_api).await.unwrap_or_default();
    let result = async {
        loop {
            let theme = Theme::at(state.theme_index);
            sync_directory_watcher(
                directory_watcher.as_mut(),
                &mut watched_directory,
                (state.overlay == Some(Overlay::FileBrowser))
                    .then(|| std::path::PathBuf::from(&state.file_browser.current_dir)),
            );
            schedule_chat_previews(
                &state,
                &image_picker,
                &mut chat_previews,
                &mut preview_pending,
                &mut preview_scope,
                backend_tx.clone(),
            );
            terminal.draw(|frame| {
                render_with_previews(frame, &state, &theme, &metrics, &mut chat_previews)
            })?;
            tokio::select! {
                _ = redraw.tick() => {
                    metrics.animation_tick = metrics.animation_tick.wrapping_add(1);
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
                    system.refresh_cpu_usage();
                    system.refresh_memory();
                    let loaded_models = std::mem::take(&mut metrics.loaded_models);
                    metrics = runtime_metrics(&system, metrics.animation_tick);
                    metrics.loaded_models = loaded_models;
                }
                _ = model_refresh.tick() => {
                    if let Ok(models) = load_ollama_models(&model_api).await {
                        metrics.loaded_models = models;
                    }
                }
                _ = preferences_refresh.tick() => {
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
                    if let Some(command) = handle_event(&mut state, terminal_event) {
                        spawn_command(
                            Arc::clone(&client),
                            model_api.clone(),
                            command,
                            backend_tx.clone(),
                        );
                    }
                }
                Some(message) = backend_rx.recv() => {
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
                                chat_previews.sort_by_key(|item| citation_preview_position(&state, &item.pdf_path, item.page));
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
                    update(&mut state, Action::QuitRequested);
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
    }
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

async fn load_ollama_models(api: &ModelApi) -> Result<Vec<LoadedModel>> {
    let response = api
        .request(reqwest::Method::GET, "/v1/models/runtime")?
        .timeout(Duration::from_secs(1))
        .send()
        .await?
        .error_for_status()?
        .json::<OllamaProcesses>()
        .await?;
    Ok(response
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
        .collect())
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

async fn pull_model(api: &ModelApi, model: String, tx: mpsc::Sender<BackendMessage>) {
    let result = async {
        let mut response = api
            .request(reqwest::Method::POST, "/v1/models/pull")?
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
            update(state, Action::BackendConnected(meta));
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
    let detail_documents = documents.clone();
    let details = tokio::task::spawn_blocking(move || inspect_documents(&detail_documents))
        .await
        .unwrap_or_default();
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
                question,
                evidence_mode,
            } => BackendMessage::RunStarted(
                client
                    .start_run(workspace, RunRequest::question(question, evidence_mode))
                    .await
                    .map(|run| run.id)
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::CancelRun(run_id) => BackendMessage::RunCancelled(
                client
                    .cancel_run(run_id)
                    .await
                    .map(|_| ())
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::Search { workspace, request } => BackendMessage::SearchCompleted(
                client
                    .search(workspace, request)
                    .await
                    .map_err(|error| error.to_string()),
            ),
            UiCommand::Ingest { workspace, request } => BackendMessage::ImportAccepted(
                client
                    .ingest(workspace, request, Uuid::new_v4().to_string())
                    .await
                    .map(|result| result.id)
                    .map_err(|error| error.to_string()),
            ),
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
                pull_model(&model_api, model, tx.clone()).await;
                return;
            }
            UiCommand::PullPackage { name, models } => {
                let _ = tx
                    .send(BackendMessage::ModelTransfer(ModelTransfer {
                        model: name,
                        status: format!("installing {} models", models.len()),
                        completed: 0,
                        total: 0,
                    }))
                    .await;
                for model in models {
                    pull_model(&model_api, model, tx.clone()).await;
                }
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
            UiCommand::AnalyzeImport { selected, existing } => {
                let result =
                    tokio::task::spawn_blocking(move || analyze_import(&selected, &existing))
                        .await
                        .unwrap_or_else(|error| ImportPreflight {
                            error: Some(error.to_string()),
                            ..ImportPreflight::default()
                        });
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
                    if !std::path::Path::new(&document.source).is_file() {
                        return Err("Original PDF is unavailable; restore was not started.".into());
                    }
                    client
                        .ingest(
                            workspace.clone(),
                            omarag_domain::IngestRequest::file(document.source.clone()),
                            Uuid::new_v4().to_string(),
                        )
                        .await
                        .map_err(|error| error.to_string())?;
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
            UiCommand::CopyText(value) => BackendMessage::ExternalOpened(copy_text(&value)),
        };
        let _ = tx.send(message).await;
    });
}

fn open_pdf(path: &str, page: Option<u32>) -> Result<(), String> {
    if !std::path::Path::new(path).exists() {
        return Err(format!("PDF no longer exists: {path}"));
    }
    if let Some(page) = page
        && Command::new("evince")
            .args(["--page-index", &page.saturating_sub(1).to_string(), path])
            .spawn()
            .is_ok()
    {
        return Ok(());
    }
    Command::new("xdg-open")
        .arg(path)
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
    let cache = std::env::temp_dir().join("oracle-of-daedalus-previews");
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

fn citation_preview_targets(state: &AppState) -> Vec<CitationPreviewTarget> {
    let mut targets = Vec::new();
    for citation in &state.chat.citations {
        let Some(page) = citation.pages.first().copied() else {
            continue;
        };
        let Some(path) = citation_source_path(state, citation) else {
            continue;
        };
        if !targets
            .iter()
            .any(|target: &CitationPreviewTarget| target.path == path && target.page == page)
        {
            let source_title = citation.document_title.as_deref().unwrap_or("Source");
            let preview_title = if citation.picture_refs.is_empty() {
                format!("{source_title} · p.{page}")
            } else {
                format!("Figure · {source_title} · p.{page}")
            };
            targets.push(CitationPreviewTarget {
                path,
                page,
                title: preview_title,
                primary_anchors: citation.primary_anchors.clone(),
                context_anchors: citation.context_anchors.clone(),
            });
        }
        if targets.len() == 4 {
            break;
        }
    }
    targets
}

fn schedule_chat_previews(
    state: &AppState,
    picker: &Picker,
    previews: &mut Vec<ChatImagePreview>,
    pending: &mut BTreeSet<(String, u32)>,
    scope: &mut PreviewScope,
    tx: mpsc::Sender<BackendMessage>,
) {
    let targets = citation_preview_targets(state);
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
            path,
            page,
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
        tokio::spawn(async move {
            let render = tokio::task::spawn_blocking(move || {
                let image_path =
                    render_pdf_page_with_anchors(&path, page, &primary_anchors, &context_anchors)?;
                let image = ImageReader::open(image_path)
                    .map_err(|error| error.to_string())?
                    .decode()
                    .map_err(|error| error.to_string())?;
                Ok(ChatImagePreview::new(
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

fn citation_preview_position(state: &AppState, path: &str, page: u32) -> usize {
    citation_preview_targets(state)
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

fn inspect_documents(
    documents: &[omarag_domain::DocumentSummary],
) -> BTreeMap<String, DocumentInsight> {
    documents
        .iter()
        .map(|document| {
            let path = std::path::Path::new(&document.source);
            let size_bytes = path.metadata().map_or(0, |metadata| metadata.len());
            let pages = pdf_info(&document.source)
                .map(|(_, pages)| pages)
                .filter(|pages| *pages > 0);
            let sha256 = std::fs::File::open(path).ok().and_then(|mut file| {
                let mut hasher = Sha256::new();
                std::io::copy(&mut file, &mut hasher).ok()?;
                Some(format!("{:x}", hasher.finalize()))
            });
            (
                document.id.clone(),
                DocumentInsight {
                    size_bytes,
                    pages,
                    sha256,
                    chunks: None,
                },
            )
        })
        .collect()
}

fn copy_text(value: &str) -> Result<(), String> {
    let mut child = Command::new("wl-copy")
        .stdin(Stdio::piped())
        .spawn()
        .or_else(|_| {
            Command::new("xclip")
                .args(["-selection", "clipboard"])
                .stdin(Stdio::piped())
                .spawn()
        })
        .map_err(|error| format!("No clipboard helper available: {error}"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| "Clipboard input unavailable".to_string())?
        .write_all(value.as_bytes())
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn export_chat(workspace: &str, session: &omarag_app::ChatSession) -> Result<(), String> {
    let directory = oracle_data_dir().join("exports");
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

fn oracle_config_dir() -> std::path::PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|home| std::path::PathBuf::from(home).join(".config"))
        })
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("oracle-of-daedalus")
}

fn oracle_data_dir() -> std::path::PathBuf {
    std::env::var_os("XDG_DATA_HOME")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|home| std::path::PathBuf::from(home).join(".local/share"))
        })
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("oracle-of-daedalus")
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
    if state.chat.question.value.trim().is_empty() || state.chat.answer.trim().is_empty() {
        return;
    }
    let session = omarag_app::ChatSession {
        workspace_id: workspace.clone(),
        question: state.chat.question.value.clone(),
        answer: state.chat.answer.clone(),
        citations: state.chat.citations.clone(),
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
        BackendMessage::ImportAnalyzed(preflight) => {
            state.library.preflight = preflight;
        }
        BackendMessage::DocumentDeleted(result) => match result {
            Ok(document) => {
                state.documents.retain(|item| item.id != document.id);
                state.undo = Some(UndoAction::RemovedDocument(document));
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
        BackendMessage::PreviewLoaded { .. } => {
            unreachable!("preview messages are handled in the event loop")
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
            Ok(hits) => {
                update(state, Action::SearchCompleted(hits));
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
            focus: omarag_app::FocusPanel::Sources,
            ..AppState::default()
        };
        state.file_browser.current_dir = "/tmp/docs".into();
        state.library.filter = omarag_app::LibraryFilter::Duplicates;
        let encoded = serde_json::to_string(&state.preferences()).unwrap();
        let mut restored = AppState::default();
        restored.apply_preferences(serde_json::from_str(&encoded).unwrap());
        assert_eq!(restored.theme_index, 2);
        assert_eq!(restored.focus, omarag_app::FocusPanel::Sources);
        assert_eq!(
            restored.library.filter,
            omarag_app::LibraryFilter::Duplicates
        );
    }
}
