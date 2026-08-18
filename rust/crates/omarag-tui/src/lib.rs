pub mod icons;
pub mod input;
pub mod progress;
pub mod theme;

use icons::Icon;
use input::{filtered_palette_commands, fuzzy_score};
use omarag_app::{
    AppState, ChatTextSelection, ConnectionState, EditorState, FocusPane, IconSet, InputMode,
    InteractionLevel, LibraryFilter, LibrarySort, ModelCatalogEntry, ModelFit, ModelPackage,
    ModelSource, Overlay, PrimarySection, View, WorkspaceProfile,
};
#[cfg(test)]
use omarag_app::{ModelCategory, ModelQuantization};
use omarag_domain::{
    AnswerCacheStatus, HardwareProfileResponse, HardwareTier, JobSnapshot, JobStatus,
    MediaEvidence, PerformanceProfile, RunId, SourceCheck, VisualEvidenceResponse,
};
use pulldown_cmark::{
    Event as MarkdownEvent, HeadingLevel, Options as MarkdownOptions, Parser, Tag, TagEnd,
};
use ratatui::{
    Frame,
    layout::{Alignment, Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span, Text},
    widgets::{
        Block, BorderType, Borders, Clear, LineGauge, List, ListItem, ListState, Paragraph, Tabs,
        Wrap,
    },
};
use ratatui_image::{
    StatefulImage,
    protocol::StatefulProtocol,
    thread::{ResizeRequest, ResizeResponse, ThreadProtocol},
};
use ratatui_textarea::{CursorMove, TextArea};
use regex::Regex;
use std::{
    fs,
    path::PathBuf,
    sync::{OnceLock, RwLock},
};
pub use theme::{Modal, Region, StatusColors, ThemeMode, ThemeSource};
use unicode_width::UnicodeWidthChar;

/// A palette plus the region roles the shell renders with.
///
/// The flat fields (`background`, `text`, the six accents…) are the base layer
/// every widget may fall back to. `sidebar`/`workspace`/`footer`/`modal` carry
/// the per-region borders and selections that make the panels read apart.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Theme {
    pub name: &'static str,
    pub source: ThemeSource,
    pub mode: ThemeMode,
    pub background: Color,
    pub panel: Color,
    pub text: Color,
    pub muted: Color,
    pub border: Color,
    pub focus: Color,
    pub cyan: Color,
    pub green: Color,
    pub yellow: Color,
    pub red: Color,
    pub purple: Color,
    pub orange: Color,
    pub selection: Color,
    pub sidebar: Region,
    pub workspace: Region,
    pub footer: Region,
    pub modal: Modal,
    pub status_colors: StatusColors,
    /// Two stops for progress bars, from the theme's own gradient.
    pub gradient: [Color; 2],
}

impl Theme {
    /// Number of selectable themes. Dynamic: the bundled set plus any user
    /// themes plus the live desktop slot.
    pub fn count() -> usize {
        theme::theme_count()
    }

    pub fn at(index: usize) -> Self {
        theme::theme_at(index)
    }

    /// Index of a theme by name, so a saved preference survives the list
    /// changing underneath it.
    pub fn index_of(name: &str) -> Option<usize> {
        theme::index_of(name)
    }

    pub fn all() -> Vec<Self> {
        theme::all_themes()
    }

    /// Theme files that failed to load, for reporting once at startup.
    pub fn problems() -> Vec<String> {
        theme::problems()
    }

    /// Refresh the palette used by the live Omarchy slot.
    ///
    /// Omarchy exposes its active theme through
    /// `~/.config/omarchy/current/theme/colors.toml`. Reading the file directly
    /// avoids spawning a process on every redraw and also works when `omarchy`
    /// is not on PATH (for example inside a portable AppImage).
    pub fn refresh_omarchy() -> bool {
        let Some(theme) = load_omarchy_theme() else {
            return Self::omarchy_available();
        };
        if let Ok(mut cached) = omarchy_theme_cache().write() {
            *cached = Some(theme);
        }
        theme::refresh_system_slot(theme);
        true
    }

    pub fn omarchy_available() -> bool {
        omarchy_theme_cache()
            .read()
            .is_ok_and(|theme| theme.is_some())
    }
}

impl Default for Theme {
    fn default() -> Self {
        Self::at(0)
    }
}

/// Semantic style vocabulary.
///
/// Every widget draws from these roles rather than reaching for a raw colour, so
/// the whole interface stays on one accent plus conventional status colours. The
/// three states a reader has to tell apart — which pane owns the keyboard, which
/// row is selected, and what a value means — are expressed by weight and marker
/// as well as by colour, so the UI survives a monochrome terminal.
impl Theme {
    /// Border of a modal or editor: accent while focused, quiet otherwise.
    pub fn border_style(&self, focused: bool) -> Style {
        Style::default().fg(if focused { self.focus } else { self.border })
    }

    /// Section or view heading.
    pub fn heading(&self) -> Style {
        Style::default().fg(self.focus).add_modifier(Modifier::BOLD)
    }

    /// Primary name of a row, dialog, or item.
    pub fn title(&self) -> Style {
        Style::default().fg(self.text).add_modifier(Modifier::BOLD)
    }

    /// Selected row: tinted background, no extra weight.
    pub fn selected(&self) -> Style {
        Style::default().bg(self.selection)
    }

    /// Body text.
    pub fn body(&self) -> Style {
        Style::default().fg(self.text)
    }

    /// Metadata, helper copy, and anything the eye should skip first.
    pub fn meta(&self) -> Style {
        Style::default().fg(self.muted)
    }

    /// Heading of a pane: accented and bold while the pane owns the keyboard.
    pub fn pane_title(&self, focused: bool) -> Style {
        if focused {
            Style::default().fg(self.focus).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(self.muted).add_modifier(Modifier::BOLD)
        }
    }

    /// Quiet label introducing a group of rows inside a pane.
    pub fn section(&self) -> Style {
        Style::default().fg(self.muted)
    }

    /// Hairline between regions.
    pub fn rule(&self) -> Style {
        Style::default().fg(self.rule_color())
    }

    /// A keyboard key in a hint.
    pub fn key(&self) -> Style {
        Style::default().fg(self.focus).add_modifier(Modifier::BOLD)
    }

    /// Selected row treatment. An unfocused pane keeps its selection legible but
    /// drops the fill, so only one pane ever looks active.
    pub fn selection_style(&self, focused: bool) -> Style {
        if focused {
            Style::default()
                .fg(self.text)
                .bg(self.selection)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(self.text)
        }
    }

    /// Leading marker for the selected row, so selection reads without colour.
    /// Never `│`: that glyph is the pane separator and must mean one thing.
    pub const fn selection_marker(focused: bool) -> &'static str {
        if focused { "▌ " } else { "· " }
    }

    /// Hairlines carry the whole three-region structure, so they need enough
    /// contrast to actually be seen — especially on light palettes. This mix
    /// keeps every bundled theme above roughly 2.4:1 against its background,
    /// which reads as a definite edge without competing with text.
    pub fn rule_color(&self) -> Color {
        mix_color(self.border, self.muted, 70)
    }

    /// Conventional status colour. Never the only carrier of meaning.
    pub fn status(&self, level: StatusLevel) -> Style {
        Style::default().fg(match level {
            StatusLevel::Ok => self.green,
            StatusLevel::Busy => self.yellow,
            StatusLevel::Warn => self.orange,
            StatusLevel::Error => self.red,
            StatusLevel::Idle => self.muted,
        })
    }
}

/// Conventional meaning of a status colour.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatusLevel {
    Ok,
    Busy,
    Warn,
    Error,
    Idle,
}

#[cfg_attr(not(test), allow(dead_code))]
const fn rgb(hex: u32) -> Color {
    Color::Rgb(
        ((hex >> 16) & 0xff) as u8,
        ((hex >> 8) & 0xff) as u8,
        (hex & 0xff) as u8,
    )
}

fn omarchy_theme_cache() -> &'static RwLock<Option<Theme>> {
    static CACHE: OnceLock<RwLock<Option<Theme>>> = OnceLock::new();
    CACHE.get_or_init(|| RwLock::new(None))
}

/// The live desktop slot, or a neutral stand-in when Omarchy is not installed.
pub(crate) fn omarchy_theme() -> Theme {
    omarchy_theme_cache()
        .read()
        .ok()
        .and_then(|theme| *theme)
        .unwrap_or_else(omarchy_fallback)
}

fn omarchy_fallback() -> Theme {
    let mut theme = Theme::parse(
        include_str!("../../../../assets/themes/catppuccin.toml"),
        ThemeSource::System,
    )
    .expect("fallback theme parses");
    theme.name = "Omarchy System";
    theme.source = ThemeSource::System;
    theme
}

fn omarchy_theme_path() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))?;
    Some(base.join("omarchy").join("current").join("theme"))
}

fn load_omarchy_theme() -> Option<Theme> {
    let directory = omarchy_theme_path()?;
    let palette = fs::read_to_string(directory.join("colors.toml")).ok()?;
    parse_omarchy_palette(&palette, directory.join("light.mode").is_file())
}

/// Builds the live theme from Omarchy's flat palette.
///
/// The same derivation is applied by `scripts/import_themes.py` to the bundled
/// Omarchy themes, so a static Omarchy theme and the live one look identical.
fn parse_omarchy_palette(palette: &str, light: bool) -> Option<Theme> {
    let mut values = std::collections::BTreeMap::new();
    for line in palette.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let value = value.split(" #").next().unwrap_or(value);
        if let Some(colour) = theme::parse_hex(value) {
            values.insert(key.trim().to_owned(), colour);
        }
    }

    let background = *values.get("background")?;
    let text = *values.get("foreground")?;
    let accent = values
        .get("accent")
        .or_else(|| values.get("blue"))
        .or_else(|| values.get("color4"))
        .copied()?;
    let pick = |key: &str, fallback: Color| values.get(key).copied().unwrap_or(fallback);

    let muted = pick("muted", mix_color(background, text, 38));
    let border = mix_color(background, text, 25);
    let selection_source = pick(
        "selection",
        mix_color(background, accent, if light { 28 } else { 18 }),
    );
    // A fill equal to the ground would hide the selected row.
    let selection = if selection_source == background {
        mix_color(background, accent, 22)
    } else {
        selection_source
    };
    let surface = pick("lighter_background", mix_color(background, text, 6));
    let dark_bg = pick("dark_background", background);
    let magenta = pick("magenta", pick("color5", accent));

    let region = |bg: Color| Region {
        fg: text,
        bg,
        border,
        border_active: accent,
        title: accent,
        item_selected_fg: text,
        item_selected_bg: selection,
    };

    Some(Theme {
        name: "Omarchy System",
        source: ThemeSource::System,
        mode: if light {
            ThemeMode::Light
        } else {
            ThemeMode::Dark
        },
        background,
        panel: surface,
        text,
        muted,
        border,
        focus: accent,
        selection,
        cyan: pick("cyan", pick("color6", accent)),
        green: pick("green", pick("color2", accent)),
        yellow: pick("yellow", pick("color3", accent)),
        red: pick("red", pick("color1", accent)),
        purple: magenta,
        orange: pick("orange", pick("color9", pick("color3", accent))),
        sidebar: region(dark_bg),
        workspace: region(background),
        footer: Region {
            fg: pick("dark_foreground", muted),
            bg: dark_bg,
            ..region(dark_bg)
        },
        modal: Modal {
            fg: text,
            bg: surface,
            border_active: accent,
            cancel_fg: background,
            cancel_bg: pick("red", accent),
            confirm_fg: background,
            confirm_bg: pick("green", accent),
        },
        status_colors: StatusColors {
            cursor: accent,
            correct: pick("green", accent),
            error: pick("red", accent),
            hint: pick("cyan", accent),
            cancel: pick("orange", muted),
            hotkey: accent,
        },
        gradient: [
            accent,
            if magenta == accent {
                pick("cyan", accent)
            } else {
                magenta
            },
        ],
    })
}

fn mix_color(left: Color, right: Color, right_percent: u16) -> Color {
    let (Color::Rgb(lr, lg, lb), Color::Rgb(rr, rg, rb)) = (left, right) else {
        return left;
    };
    let blend = |left: u8, right: u8| -> u8 {
        let left = u16::from(left);
        let right = u16::from(right);
        ((left * (100 - right_percent) + right * right_percent) / 100) as u8
    };
    Color::Rgb(blend(lr, rr), blend(lg, rg), blend(lb, rb))
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LoadedModel {
    pub name: String,
    pub size: u64,
    pub size_vram: u64,
    pub context_length: u64,
    pub parameter_size: String,
    pub quantization: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ModelRoleStatus {
    pub role: String,
    pub model: Option<String>,
    pub residency: String,
    pub shared_with: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct RuntimeMetrics {
    pub cpu_usage: f32,
    pub cpu_count: usize,
    pub memory_used: u64,
    pub memory_total: u64,
    pub memory_available: u64,
    pub gpu_name: Option<String>,
    pub vram_used: u64,
    pub vram_total: u64,
    pub shared_gpu_memory: u64,
    pub animation_tick: u64,
    pub loaded_models: Vec<LoadedModel>,
    pub model_roles: Vec<ModelRoleStatus>,
}

pub struct ChatImagePreview {
    pub citation_index: usize,
    pub page_index: usize,
    pub pdf_path: String,
    pub page: u32,
    pub title: String,
    pub protocol: ThreadProtocol,
    response_rx: std::sync::mpsc::Receiver<ResizeResponse>,
}

pub struct MediaImagePreview {
    pub media_id: String,
    pub protocol: ThreadProtocol,
    response_rx: std::sync::mpsc::Receiver<ResizeResponse>,
}

impl MediaImagePreview {
    pub fn new(media_id: String, protocol: StatefulProtocol) -> Self {
        let (request_tx, request_rx) = std::sync::mpsc::channel::<ResizeRequest>();
        let (response_tx, response_rx) = std::sync::mpsc::channel::<ResizeResponse>();
        std::thread::spawn(move || {
            while let Ok(request) = request_rx.recv() {
                if let Ok(response) = request.resize_encode() {
                    let _ = response_tx.send(response);
                }
            }
        });
        Self {
            media_id,
            protocol: ThreadProtocol::new(request_tx, Some(protocol)),
            response_rx,
        }
    }

    fn receive_resizes(&mut self) {
        while let Ok(response) = self.response_rx.try_recv() {
            self.protocol.update_resized_protocol(response);
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum VisualInspectorTab {
    #[default]
    Pages,
    Figures,
    Sources,
}

impl VisualInspectorTab {
    pub const ALL: [Self; 3] = [Self::Pages, Self::Figures, Self::Sources];

    pub const fn label(self) -> &'static str {
        match self {
            Self::Pages => "Pages",
            Self::Figures => "Figures",
            Self::Sources => "Sources",
        }
    }

    pub const fn next(self) -> Self {
        match self {
            Self::Pages => Self::Figures,
            Self::Figures => Self::Sources,
            Self::Sources => Self::Pages,
        }
    }

    pub const fn previous(self) -> Self {
        match self {
            Self::Pages => Self::Sources,
            Self::Figures => Self::Pages,
            Self::Sources => Self::Figures,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct VisualInspectorState {
    pub run_id: Option<RunId>,
    pub evidence: VisualEvidenceResponse,
    pub tab: VisualInspectorTab,
    pub selected_media: usize,
    pub sources_collapsed: bool,
    /// True when the backend does not implement the V1.1 endpoint. Citation
    /// pages remain available, but the figures section intentionally stays empty.
    pub legacy: bool,
}

impl VisualInspectorState {
    pub fn replace(&mut self, run_id: RunId, evidence: VisualEvidenceResponse) {
        self.run_id = Some(run_id);
        self.evidence = evidence.normalized();
        self.selected_media = self
            .selected_media
            .min(self.evidence.media.len().saturating_sub(1));
        self.legacy = false;
    }

    pub fn use_legacy(&mut self, run_id: RunId) {
        self.run_id = Some(run_id);
        self.evidence = VisualEvidenceResponse::default();
        self.selected_media = 0;
        self.legacy = true;
    }

    pub fn clear(&mut self) {
        *self = Self::default();
    }
}

pub fn performance_profile(profile: omarag_app::HardwareProfile) -> PerformanceProfile {
    match profile {
        omarag_app::HardwareProfile::Eco => PerformanceProfile::Fast,
        omarag_app::HardwareProfile::Laptop => PerformanceProfile::Normal,
        omarag_app::HardwareProfile::Quality => PerformanceProfile::Quality,
    }
}

pub fn fallback_hardware_profile(metrics: &RuntimeMetrics) -> HardwareProfileResponse {
    let tier = HardwareTier::for_capacity(metrics.memory_total, metrics.vram_total);
    let limiting_factor = if metrics.memory_total < 16 * 1_073_741_824 {
        "system memory"
    } else if metrics.vram_total == 0 {
        "accelerator / VRAM"
    } else if metrics.vram_total < 24 * 1_073_741_824 {
        "VRAM"
    } else if metrics.cpu_count < 8 {
        "CPU"
    } else {
        "model memory"
    };
    HardwareProfileResponse {
        tier,
        tier_label: format!("Local tier {}", tier.level()),
        limiting_factor: limiting_factor.into(),
        catalog_version: format!("bundled-{}", env!("CARGO_PKG_VERSION")),
        ..HardwareProfileResponse::default()
    }
}

impl ChatImagePreview {
    pub fn new(
        citation_index: usize,
        page_index: usize,
        pdf_path: String,
        page: u32,
        title: String,
        protocol: StatefulProtocol,
    ) -> Self {
        let (request_tx, request_rx) = std::sync::mpsc::channel::<ResizeRequest>();
        let (response_tx, response_rx) = std::sync::mpsc::channel::<ResizeResponse>();
        std::thread::spawn(move || {
            while let Ok(request) = request_rx.recv() {
                if let Ok(response) = request.resize_encode() {
                    let _ = response_tx.send(response);
                }
            }
        });
        Self {
            citation_index,
            page_index,
            pdf_path,
            page,
            title,
            protocol: ThreadProtocol::new(request_tx, Some(protocol)),
            response_rx,
        }
    }

    fn receive_resizes(&mut self) {
        while let Ok(response) = self.response_rx.try_recv() {
            self.protocol.update_resized_protocol(response);
        }
    }
}

pub fn render(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    render_with_metrics(frame, state, theme, &RuntimeMetrics::default());
}

pub fn render_with_metrics(
    frame: &mut Frame<'_>,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    render_with_previews(frame, state, theme, metrics, &mut []);
}

pub fn render_with_previews(
    frame: &mut Frame<'_>,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    previews: &mut [ChatImagePreview],
) {
    let hardware = fallback_hardware_profile(metrics);
    render_with_runtime(
        frame,
        state,
        theme,
        metrics,
        previews,
        &mut [],
        &VisualInspectorState::default(),
        &hardware,
    );
}

#[allow(clippy::too_many_arguments)]
pub fn render_with_runtime(
    frame: &mut Frame<'_>,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    previews: &mut [ChatImagePreview],
    media_previews: &mut [MediaImagePreview],
    visual: &VisualInspectorState,
    hardware: &HardwareProfileResponse,
) {
    frame.render_widget(
        Block::default().style(Style::default().bg(theme.background).fg(theme.text)),
        frame.area(),
    );
    if frame.area().width < 80 || frame.area().height < 24 {
        render_minimum_size(frame, frame.area(), theme);
        return;
    }
    let [header, body, footer] = screen_areas(frame.area());
    let areas = app_areas(body, state.focus_pane);
    render_header(
        frame,
        header,
        state,
        theme,
        metrics,
        areas.sidebar.width > 0,
    );
    if areas.sidebar.width > 0 {
        render_sidebar(frame, areas.sidebar, state, theme, metrics);
    }
    if areas.workspace.width > 0 {
        render_workspace(
            frame,
            areas.workspace,
            state,
            theme,
            metrics,
            previews,
            hardware,
        );
    }
    if areas.inspector.width > 0 {
        let mut inspector_runtime = InspectorRenderContext {
            previews,
            media_previews,
            visual,
            hardware,
            compact_shell: frame.area().width < 120 || frame.area().height < 34,
        };
        render_inspector(
            frame,
            areas.inspector,
            state,
            theme,
            metrics,
            &mut inspector_runtime,
        );
    }
    render_footer(frame, footer, state, theme);
    render_overlay(frame, state, theme);
}

/// One row of identity and live state, then a hairline. This is the only global
/// chrome above the workspace: product, where you are, which library you are in,
/// and what the machine is doing right now.
fn render_header(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    sidebar_visible: bool,
) {
    let row = Rect::new(area.x, area.y, area.width, 1);

    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(" OmaRag", theme.title()),
            Span::styled("   ", theme.meta()),
            Span::styled(state.view.label(), Style::default().fg(theme.focus)),
        ])),
        row,
    );
    frame.render_widget(
        Paragraph::new(header_status(
            state,
            theme,
            metrics,
            row.width,
            sidebar_visible,
        ))
        .alignment(Alignment::Right),
        row,
    );
}

/// Right side of the header: the active library, then whatever the backend is
/// busy with. Progress is only shown when the backend actually reports it.
fn header_status(
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    width: u16,
    sidebar_visible: bool,
) -> Line<'static> {
    let mut spans = Vec::new();
    // The sidebar owns the library name; the header only repeats it when the
    // terminal is too narrow for the sidebar to be on screen.
    if !sidebar_visible
        && let Some(library) = state
            .active_workspace
            .as_ref()
            .and_then(|id| state.workspaces.iter().find(|item| &item.id == id))
    {
        spans.push(Span::styled(
            truncate(&library.name, (width / 3).max(12) as usize),
            theme.body(),
        ));
    }

    let (symbol, label, level) = match &state.connection {
        ConnectionState::Disconnected { .. } => {
            ("×", "disconnected".to_owned(), StatusLevel::Error)
        }
        _ => match header_activity(state) {
            HeaderActivity::Answering => (
                spinner(metrics.animation_tick),
                nonempty(header_phase_label(state), "answering").to_owned(),
                StatusLevel::Busy,
            ),
            HeaderActivity::Indexing { percent } => (
                spinner(metrics.animation_tick),
                format!("indexing {percent}%"),
                StatusLevel::Busy,
            ),
            HeaderActivity::Idle => ("·", "ready".to_owned(), StatusLevel::Ok),
        },
    };
    if !spans.is_empty() {
        spans.push(Span::styled("   ", theme.meta()));
    }
    spans.push(Span::styled(format!("{symbol} "), theme.status(level)));
    spans.push(Span::styled(
        format!("{} ", truncate(&label.to_lowercase(), 28)),
        theme.meta(),
    ));
    Line::from(spans)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum HeaderActivity {
    Idle,
    Answering,
    Indexing { percent: u8 },
}

fn header_activity(state: &AppState) -> HeaderActivity {
    if state.chat.request_pending || state.chat.active_run.is_some() {
        return HeaderActivity::Answering;
    }
    let Some(job) = active_index_job(state) else {
        return HeaderActivity::Idle;
    };
    let progress = if job.progress.is_finite() {
        job.progress.clamp(0.0, 1.0)
    } else {
        0.0
    };
    HeaderActivity::Indexing {
        percent: (progress * 100.0).round() as u8,
    }
}

fn active_index_job(state: &AppState) -> Option<&JobSnapshot> {
    let active_workspace = state.active_workspace.as_deref();
    state
        .jobs
        .values()
        .filter(|job| {
            !is_terminal(&job.status)
                && (job.kind.eq_ignore_ascii_case("ingest")
                    || job.kind.to_ascii_lowercase().contains("index"))
                && active_workspace.is_none_or(|workspace| job.workspace_id == workspace)
        })
        .max_by(|left, right| {
            index_activity_priority(&left.status)
                .cmp(&index_activity_priority(&right.status))
                .then_with(|| left.updated_at.cmp(&right.updated_at))
        })
}

fn index_activity_priority(status: &JobStatus) -> u8 {
    match status {
        JobStatus::Running | JobStatus::PauseRequested => 2,
        JobStatus::Queued => 1,
        JobStatus::Paused | JobStatus::Completed | JobStatus::Cancelled | JobStatus::Failed => 0,
    }
}

fn header_phase_label(state: &AppState) -> &str {
    if state.chat.request_pending || state.chat.active_run.is_some() {
        return &state.chat.phase_label;
    }
    active_index_job(state).map_or("indexing", |job| job.phase.as_str())
}

pub(crate) fn screen_areas(area: Rect) -> [Rect; 3] {
    Layout::vertical([
        Constraint::Length(1),
        Constraint::Fill(1),
        Constraint::Length(1),
    ])
    .areas(area)
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct AppAreas {
    pub sidebar: Rect,
    pub workspace: Rect,
    pub inspector: Rect,
}

/// Widest comfortable measure for the workspace. Long-form answers are the
/// product's main reading surface, and lines much beyond this get hard to track
/// back to the next line.
const WORKSPACE_MAX_WIDTH: u16 = 96;

/// The evidence column absorbs surplus width up to this point: page titles and
/// citation lines genuinely read better with more room.
const INSPECTOR_MAX_WIDTH: u16 = 52;

pub(crate) fn app_areas(area: Rect, focus: FocusPane) -> AppAreas {
    if area.width >= 120 {
        // Prose has a comfortable maximum measure; structured evidence does
        // not. Surplus width past a readable workspace goes to the evidence
        // column, then to a trailing gutter — never into ever-longer answer
        // lines, which are the hardest thing in the product to read.
        let free = area.width.saturating_sub(24 + 1 + 1);
        let inspector_width = free
            .saturating_sub(WORKSPACE_MAX_WIDTH)
            .clamp(38, INSPECTOR_MAX_WIDTH);
        let workspace_width = free
            .saturating_sub(inspector_width)
            .min(WORKSPACE_MAX_WIDTH);
        let [sidebar, _, workspace, _, inspector, _gutter] = Layout::horizontal([
            Constraint::Length(24),
            Constraint::Length(1),
            Constraint::Length(workspace_width),
            Constraint::Length(1),
            Constraint::Length(inspector_width),
            Constraint::Fill(1),
        ])
        .areas(area);
        AppAreas {
            sidebar,
            workspace,
            inspector,
        }
    } else if area.width >= 96 {
        let [sidebar, _, stage] = Layout::horizontal([
            Constraint::Length(22),
            Constraint::Length(1),
            Constraint::Fill(1),
        ])
        .areas(area);
        if focus == FocusPane::Inspector {
            AppAreas {
                sidebar,
                inspector: stage,
                ..AppAreas::default()
            }
        } else {
            AppAreas {
                sidebar,
                workspace: stage,
                ..AppAreas::default()
            }
        }
    } else {
        match focus {
            FocusPane::Sidebar => AppAreas {
                sidebar: area,
                ..AppAreas::default()
            },
            FocusPane::Workspace => AppAreas {
                workspace: area,
                ..AppAreas::default()
            },
            FocusPane::Inspector => AppAreas {
                inspector: area,
                ..AppAreas::default()
            },
        }
    }
}

fn render_minimum_size(frame: &mut Frame<'_>, area: Rect, theme: &Theme) {
    let message = centered(72, 9, area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled("OmaRag", theme.title()),
            Line::from(""),
            Line::styled("This terminal is too small to work in.", theme.body()),
            Line::styled(
                format!("Now {}×{} · needs 80×24", area.width, area.height),
                theme.meta(),
            ),
            Line::from(""),
            Line::styled("Resize the window to continue.", theme.meta()),
        ])
        .alignment(Alignment::Center),
        message,
    );
}

fn section_views(section: PrimarySection, level: InteractionLevel) -> Vec<View> {
    let candidates: &[View] = match section {
        PrimarySection::Chat => &[View::Conversation, View::History, View::Retrieval],
        PrimarySection::Library => &[
            View::Books,
            View::Indexing,
            View::Sources,
            View::Quality,
            View::Backups,
        ],
        PrimarySection::Foundry => &[View::FoundryOverview, View::Models],
        PrimarySection::Settings => &[View::Settings, View::Themes, View::System],
    };
    candidates
        .iter()
        .copied()
        .filter(|view| level == InteractionLevel::Workshop || !view.advanced())
        .collect()
}

/// A navigable view. The marker carries selection without colour; the accent and
/// the fill only appear when the sidebar actually owns the keyboard.
fn sidebar_child_line(label: &str, active: bool, focused: bool, theme: &Theme) -> Line<'static> {
    let marker = if active {
        Theme::selection_marker(focused)
    } else {
        "  "
    };
    let style = if active {
        theme.selection_style(focused)
    } else {
        theme.body()
    };
    Line::from(vec![
        Span::styled(" ", theme.meta()),
        Span::styled(
            marker,
            if active {
                Style::default().fg(theme.focus)
            } else {
                theme.meta()
            },
        ),
        Span::styled(label.to_owned(), style),
    ])
}

/// A section of the navigation. Quiet: it groups, it does not compete.
fn sidebar_heading(label: &str, active: bool, width: u16, theme: &Theme) -> Line<'static> {
    let style = if active {
        Style::default()
            .fg(theme.focus)
            .add_modifier(Modifier::BOLD)
    } else {
        theme.section()
    };
    let rule = (width as usize).saturating_sub(label.chars().count() + 1);
    Line::from(vec![
        Span::styled(label.to_owned(), style),
        Span::styled(format!(" {}", "─".repeat(rule)), theme.rule()),
    ])
}

/// One entry per rendered navigation row, in render order, so a click maps to
/// the row the reader actually sees. Must mirror `render_sidebar` exactly —
/// including the blank spacer that precedes every section after the first.
pub(crate) fn sidebar_navigation_rows(state: &AppState) -> Vec<Option<View>> {
    let mut rows = Vec::new();
    for (index, section) in PrimarySection::CORE.iter().copied().enumerate() {
        if index > 0 {
            rows.push(None); // spacer
        }
        rows.push(Some(match section {
            PrimarySection::Chat => View::Conversation,
            PrimarySection::Library => View::Books,
            PrimarySection::Foundry => View::FoundryOverview,
            PrimarySection::Settings => View::Settings,
        }));
        rows.extend(
            section_views(section, state.interaction_level)
                .into_iter()
                .map(Some),
        );
    }
    rows
}

/// Navigation pane. Quiet by design: it orients the reader and gets out of the
/// way. The library name is the pane heading, the sections are muted labels, and
/// the active view carries the only accent in the column.
fn render_sidebar(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    let focused = state.focus_pane == FocusPane::Sidebar;
    let library = state
        .active_workspace
        .as_ref()
        .and_then(|id| state.workspaces.iter().find(|item| &item.id == id))
        .map_or("No library", |item| item.name.as_str());
    render_frame(
        frame,
        area,
        library,
        &[format!("{} libraries", state.workspaces.len().max(1))],
        focused,
        theme.sidebar,
    );
    let inner = pane_inner(area);
    if inner.height == 0 {
        return;
    }

    // Navigation has first claim on the column. The status block only gets the
    // rows left over, so a machine readout can never push a view off screen.
    let status = sidebar_status_lines(state, metrics, inner.width, theme);
    let navigation_rows = sidebar_navigation_rows(state).len() as u16;
    let spare = inner.height.saturating_sub(navigation_rows);
    let status_height = (status.len() as u16 + 1).min(spare);
    let [navigation, status_area] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(status_height)]).areas(inner);

    let mut lines = Vec::new();
    let section = state.view.section();
    for (index, item) in PrimarySection::CORE.iter().copied().enumerate() {
        if index > 0 {
            lines.push(Line::raw(""));
        }
        lines.push(sidebar_heading(
            item.label(),
            item == section,
            inner.width,
            theme,
        ));
        for view in section_views(item, state.interaction_level) {
            lines.push(sidebar_child_line(
                view.label(),
                state.view == view,
                focused,
                theme,
            ));
        }
    }
    let active_row = lines
        .len()
        .saturating_sub(1)
        .min(sidebar_active_row(state) as usize) as u16;
    let scroll = active_row.saturating_sub(navigation.height.saturating_sub(1));
    frame.render_widget(Paragraph::new(lines).scroll((scroll, 0)), navigation);

    if status_height > 0 {
        let mut rows = vec![Line::raw("")];
        rows.extend(
            status
                .into_iter()
                .take(status_height.saturating_sub(1) as usize),
        );
        frame.render_widget(Paragraph::new(rows), status_area);
    }
}

/// Row index of the active view within the rendered navigation, blank spacer
/// rows included, so scrolling keeps the active item on screen.
fn sidebar_active_row(state: &AppState) -> u16 {
    let mut row = 0u16;
    for (index, item) in PrimarySection::CORE.iter().copied().enumerate() {
        // Every section after the first is preceded by a blank spacer row.
        if index > 0 {
            row += 1;
        }
        row += 1; // the section label itself
        for view in section_views(item, state.interaction_level) {
            if view == state.view {
                return row;
            }
            row += 1;
        }
    }
    row
}

/// Ambient machine state: load, memory, and which model fills each role. Kept to
/// one line per fact so navigation keeps the vertical space.
fn sidebar_status_lines(
    state: &AppState,
    metrics: &RuntimeMetrics,
    width: u16,
    theme: &Theme,
) -> Vec<Line<'static>> {
    // A short bar reads faster than two numbers when you only want to know
    // whether the machine has room left.
    let bar_width = width.saturating_sub(14).clamp(4, 10);
    let gauge = |label: &str, used: u64, total: u64, theme: &Theme| {
        let ratio = if total > 0 {
            used as f64 / total as f64
        } else {
            0.0
        };
        let mut spans = vec![Span::styled(format!(" {label:<5}"), theme.meta())];
        spans.extend(progress::bar(bar_width, ratio, theme));
        spans.push(Span::styled(
            format!(" {}", compact_memory(used)),
            theme.body(),
        ));
        Line::from(spans)
    };
    let mut cpu = vec![Span::styled(" cpu  ", theme.meta())];
    cpu.extend(progress::bar(
        bar_width,
        f64::from(metrics.cpu_usage) / 100.0,
        theme,
    ));
    cpu.push(Span::styled(
        format!(" {:.0}%", metrics.cpu_usage),
        theme.body(),
    ));
    let mut lines = vec![Line::from(cpu)];
    lines.push(gauge(
        "mem",
        metrics.memory_used,
        metrics.memory_total,
        theme,
    ));
    if metrics.vram_total > 0 {
        lines.push(gauge("vram", metrics.vram_used, metrics.vram_total, theme));
    }

    let roles: Vec<(String, Option<String>, String)> = if metrics.model_roles.is_empty() {
        configured_models(state)
            .into_iter()
            .map(|(role, model)| {
                let loaded = metrics
                    .loaded_models
                    .iter()
                    .any(|item| model_matches(&item.name, &model));
                let residency = if loaded { "loaded" } else { "idle" };
                let model = (model != "not configured").then_some(model);
                (role, model, residency.to_owned())
            })
            .collect()
    } else {
        metrics
            .model_roles
            .iter()
            .map(|role| {
                (
                    role.role.clone(),
                    role.model.clone(),
                    role.residency.clone(),
                )
            })
            .collect()
    };
    for (role, model, residency) in roles {
        let loaded = residency.eq_ignore_ascii_case("loaded");
        let label = role_label(&role);
        let model = model.map_or("—".to_owned(), |model| compact_model_name(&model));
        let budget = width.saturating_sub(label.len() as u16 + 4) as usize;
        lines.push(Line::from(vec![
            Span::styled(
                if loaded { " ● " } else { " ○ " },
                theme.status(if loaded {
                    StatusLevel::Ok
                } else {
                    StatusLevel::Idle
                }),
            ),
            Span::styled(format!("{label} "), theme.meta()),
            Span::styled(truncate(&model, budget), theme.body()),
        ]));
    }
    lines
}

fn role_label(role: &str) -> String {
    match role.to_ascii_lowercase().as_str() {
        "chat" => "chat".into(),
        "vl" | "vision" => "vision".into(),
        "embedding" | "embed" => "embed".into(),
        "rerank" | "reranker" => "rerank".into(),
        other => other.to_owned(),
    }
}

/// A list with the standard OmaRag selection treatment: a marker that reads
/// without colour, and a fill that appears only while the pane owns the keyboard.
fn selection_list<'a>(items: Vec<ListItem<'a>>, focused: bool, theme: &Theme) -> List<'a> {
    List::new(items)
        .highlight_symbol(Theme::selection_marker(focused))
        .highlight_style(theme.selection_style(focused))
}

/// Frame in the superfile idiom: rounded corners, the title cut into the top
/// edge as `─┤ Title ├───`, optional status items cut into the bottom edge, and
/// an accent border while the region owns the keyboard.
///
/// This is the one place borders are drawn, so the whole interface shares a
/// single frame language.
fn render_frame(
    frame: &mut Frame<'_>,
    area: Rect,
    title: &str,
    info: &[String],
    focused: bool,
    region: Region,
) {
    if area.width < 2 || area.height < 2 {
        return;
    }
    // Each region wears its own border colour, which is what stops three
    // stacked panels reading as one undifferentiated accent.
    let style = Style::default().fg(if focused {
        region.border_active
    } else {
        region.border
    });
    let width = area.width as usize;
    // Corners take one column each.
    let span = width.saturating_sub(2);

    let top = inset_edge(title, span, false);
    let bottom = inset_edge_items(info, span);

    let mut lines = Vec::with_capacity(area.height as usize);
    lines.push(Line::styled(format!("╭{top}╮"), style));
    for _ in 1..area.height.saturating_sub(1) {
        lines.push(Line::from(vec![
            Span::styled("│", style),
            Span::raw(" ".repeat(span)),
            Span::styled("│", style),
        ]));
    }
    lines.push(Line::styled(format!("╰{bottom}╯"), style));
    frame.render_widget(Paragraph::new(lines), area);
}

/// `─┤ Title ├──────` — the label cut into a border run.
fn inset_edge(label: &str, span: usize, centered: bool) -> String {
    if label.is_empty() || span < 6 {
        return "─".repeat(span);
    }
    // One leading border unit, then `┤ label ├`.
    let budget = span.saturating_sub(5);
    let label = truncate(label, budget);
    let used = label.chars().count() + 4;
    let remaining = span.saturating_sub(used);
    let lead = if centered { remaining / 2 } else { 1 };
    let trail = remaining.saturating_sub(lead);
    format!("{}┤ {label} ├{}", "─".repeat(lead), "─".repeat(trail))
}

/// `───┤ 3 books ├─┤ ready ├──` — status cut into the bottom edge, right aligned.
fn inset_edge_items(items: &[String], span: usize) -> String {
    let items: Vec<&String> = items.iter().filter(|item| !item.is_empty()).collect();
    if items.is_empty() || span < 8 {
        return "─".repeat(span);
    }
    let mut tail = String::new();
    for item in &items {
        let budget = span / items.len().max(1);
        if budget < 5 {
            break;
        }
        let item = truncate(item, budget.saturating_sub(4));
        tail.push_str(&format!("┤ {item} ├─"));
    }
    let used = tail.chars().count();
    if used + 2 > span {
        return "─".repeat(span);
    }
    format!("{}{tail}─", "─".repeat(span.saturating_sub(used + 1)))
}

/// Content area inside a frame: one row and one column of padding on each side.
pub(crate) fn pane_inner(area: Rect) -> Rect {
    Rect::new(
        area.x.saturating_add(2),
        area.y.saturating_add(1),
        area.width.saturating_sub(4),
        area.height.saturating_sub(2),
    )
}

/// Content area of a labelled section inside a pane: one label row on top.
/// Shared by rendering and hit-testing.
pub(crate) fn section_inner(area: Rect) -> Rect {
    Rect::new(
        area.x,
        area.y.saturating_add(1),
        area.width,
        area.height.saturating_sub(1),
    )
}

/// Width of the label column in a label/value block. One number instead of
/// counted spaces in every call site, so the columns cannot drift apart.
const LABEL_COLUMN: usize = 12;

/// `Label       value` — a row in a properties block.
fn field(label: &str, value: impl Into<String>, theme: &Theme) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("{label:<LABEL_COLUMN$}"), theme.meta()),
        Span::styled(value.into(), theme.body()),
    ])
}

/// Same, with the value carrying its own meaning colour.
fn field_styled(
    label: &str,
    value: impl Into<String>,
    style: Style,
    theme: &Theme,
) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("{label:<LABEL_COLUMN$}"), theme.meta()),
        Span::styled(value.into(), style),
    ])
}

/// Resolves an icon for the user's current settings.
fn ico(state: &AppState, what: Icon) -> String {
    icons::icon(what, state.icon_mode, state.icon_set)
}

/// A muted section label row inside a pane, with a divider running to the right
/// edge — the sidebar heading treatment superfile uses for `Pinned` and `Disks`.
fn render_section_label(frame: &mut Frame<'_>, area: Rect, label: &str, theme: &Theme) {
    let width = area.width as usize;
    let rule = width.saturating_sub(label.chars().count() + 1);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(label.to_owned(), theme.section()),
            Span::styled(format!(" {}", "─".repeat(rule)), theme.rule()),
        ])),
        Rect::new(area.x, area.y, area.width, 1),
    );
}

/// Short facts stamped into the workspace frame's bottom edge — superfile puts
/// the panel's own counters there rather than in the content.
/// Short facts for the evidence column's bottom edge.
fn inspector_status_items(state: &AppState) -> Vec<String> {
    match state.view {
        View::Conversation | View::Retrieval if !state.chat.citations.is_empty() => {
            vec![format!(
                "{}/{}",
                state.citation_cursor.saturating_add(1),
                state.chat.citations.len()
            )]
        }
        View::Books if !state.documents.is_empty() => vec![format!(
            "{}/{}",
            state
                .asset_cursor
                .saturating_add(1)
                .min(state.documents.len()),
            state.documents.len()
        )],
        _ => Vec::new(),
    }
}

fn workspace_status_items(state: &AppState) -> Vec<String> {
    match state.view {
        View::Books => vec![format!("{} books", state.documents.len())],
        View::Indexing | View::Activity => {
            let running = state
                .jobs
                .values()
                .filter(|job| !is_terminal(&job.status))
                .count();
            vec![format!("{running}/{} runs", state.jobs.len())]
        }
        View::Conversation if !state.chat.citations.is_empty() => {
            vec![format!("{} sources", state.chat.citations.len())]
        }
        View::Models => vec![format!("{} models", state.model_manager.entries.len())],
        View::FoundryOverview => vec![format!("{} presets", state.model_manager.packages.len())],
        View::Themes => vec![format!("{} palettes", Theme::count())],
        _ => Vec::new(),
    }
}

fn render_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    previews: &mut [ChatImagePreview],
    hardware: &HardwareProfileResponse,
) {
    let focused = state.focus_pane == FocusPane::Workspace;
    render_frame(
        frame,
        area,
        state.view.label(),
        &workspace_status_items(state),
        focused,
        theme.workspace,
    );
    let inner = pane_inner(area);
    if inner.width < 20 || inner.height < 4 {
        return;
    }
    match state.view {
        View::Conversation => render_chat_workspace(frame, inner, state, theme, metrics, previews),
        View::History => render_history_workspace(frame, inner, state, theme),
        View::Retrieval => render_retrieval_workspace(frame, inner, state, theme),
        View::Books => render_books_workspace(frame, inner, state, theme),
        // `Activity` is the pre-1.1 name for this screen; preferences migrate
        // it, but a stored route may still arrive here.
        View::Indexing | View::Activity => {
            render_indexing_workspace(frame, inner, state, theme, metrics)
        }
        View::Sources => render_sources_workspace(frame, inner, state, theme),
        View::Quality => render_quality_workspace(frame, inner, state, theme),
        View::Backups => render_backups_workspace(frame, inner, state, theme),
        View::FoundryOverview => {
            render_foundry_workspace(frame, inner, state, theme, metrics, hardware)
        }
        View::Models => render_models_workspace(frame, inner, state, theme, metrics),
        View::System => render_system_workspace(frame, inner, state, theme, metrics),
        View::Settings => render_settings_workspace(frame, inner, state, theme),
        View::Themes => render_themes_workspace(frame, inner, state, theme),
    }
}

/// Vertical split of the conversation pane. Shared by rendering and by mouse
/// hit-testing so text selection always lands on the glyph under the pointer.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct ChatAreas {
    /// The asked question, above the answer. Zero height when nothing was asked.
    pub question: Rect,
    /// The `OmaRag` speaker label. Kept out of `answer` so that selection
    /// offsets resolve against the answer's own first glyph row.
    pub speaker: Rect,
    /// Where the answer text is drawn; the origin selection offsets resolve against.
    pub answer: Rect,
    /// Label row above the composer.
    pub scope: Rect,
    /// The composer row, including the `›` prompt gutter.
    pub composer: Rect,
    /// Just the editable part of the composer, past the prompt gutter. Text
    /// hit-testing must use this, not `composer`.
    pub editor: Rect,
}

/// Width of the `›` prompt gutter in front of the composer.
pub(crate) const CHAT_PROMPT_WIDTH: u16 = 2;

pub(crate) fn chat_areas(inner: Rect, state: &AppState) -> ChatAreas {
    // The composer grows with the question instead of reserving three rows that
    // a one-line question leaves blank above the footer.
    let composer_height = if inner.height >= 9 {
        (state.chat.question.value.split('\n').count() as u16).clamp(1, 3)
    } else {
        1
    };
    let question = chat_question_text(state);
    let roomy = inner.height >= 12;
    let question_height = if question.is_empty() || !roomy {
        0
    } else {
        // Speaker label, the question (wrapped), and a blank separator row.
        let wrapped = question
            .chars()
            .count()
            .div_ceil(inner.width.max(1) as usize)
            .clamp(1, 3) as u16;
        2 + wrapped
    };
    let speaker_height = u16::from(roomy);
    let [question, speaker, answer, scope, composer] = Layout::vertical([
        Constraint::Length(question_height),
        Constraint::Length(speaker_height),
        Constraint::Fill(1),
        Constraint::Length(1),
        Constraint::Length(composer_height),
    ])
    .areas(inner);
    let editor = Rect::new(
        composer.x.saturating_add(CHAT_PROMPT_WIDTH),
        composer.y,
        composer.width.saturating_sub(CHAT_PROMPT_WIDTH),
        composer.height,
    );
    ChatAreas {
        question,
        speaker,
        answer,
        scope,
        composer,
        editor,
    }
}

/// The question the visible answer belongs to.
fn chat_question_text(state: &AppState) -> String {
    if !state.chat.submitted_question.trim().is_empty() {
        return state.chat.submitted_question.clone();
    }
    if state.chat.answer.is_empty() && !state.chat.request_pending {
        return String::new();
    }
    state.chat.question.value.clone()
}

/// The conversation. The question is shown with its answer so the reader can see
/// what was actually asked, the answer is the loudest text on screen, and the
/// composer is anchored at the bottom where typing already happens.
fn render_chat_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    _previews: &mut [ChatImagePreview],
) {
    let areas = chat_areas(area, state);
    let editing = state.focus_pane == FocusPane::Workspace && state.input_mode == InputMode::Text;

    if areas.question.height > 0 {
        frame.render_widget(
            Paragraph::new(vec![
                Line::styled("You", theme.section()),
                Line::styled(chat_question_text(state), theme.body()),
            ])
            .wrap(Wrap { trim: false }),
            areas.question,
        );
    }
    if areas.speaker.height > 0 && state.chat.error.is_none() {
        frame.render_widget(
            Paragraph::new(Line::styled("OmaRag", theme.section())),
            areas.speaker,
        );
    }

    let (text, already_wrapped) = if let Some(error) = &state.chat.error {
        (
            Text::from(vec![
                Line::styled("Could not answer", theme.status(StatusLevel::Error)),
                Line::from(""),
                Line::styled(error.clone(), theme.body()),
            ]),
            false,
        )
    } else if state.chat.answer.is_empty() {
        (
            Text::from(chat_placeholder_lines(state, theme, metrics)),
            false,
        )
    } else {
        let mut answer = selectable_answer(
            &state.chat.answer,
            state.citation_cursor,
            theme,
            areas.answer.width,
            state.chat.selection,
        );
        // A streaming answer must not look finished. Appending after the answer
        // keeps every existing glyph row at the offset selection resolved to.
        if state.chat.request_pending || state.chat.active_run.is_some() {
            // The draft is what the model is writing right now, before the claim
            // it belongs to has been checked against its sources. Set it apart so
            // nobody mistakes unverified prose for a cited answer.
            if !state.chat.draft.is_empty() {
                if !state.chat.answer.is_empty() {
                    answer.push_line(Line::raw(""));
                }
                // The answer arrives pre-wrapped, so the paragraph does not wrap
                // for us; the draft has to be broken to the same measure.
                for piece in wrap_plain(&state.chat.draft, areas.answer.width) {
                    answer.push_line(Line::styled(
                        piece,
                        Style::default()
                            .fg(theme.muted)
                            .add_modifier(Modifier::ITALIC),
                    ));
                }
            }
            let phase = chat_phase_display(state);
            answer.push_line(Line::from(vec![
                Span::styled(
                    format!("{} ", spinner(metrics.animation_tick)),
                    theme.status(StatusLevel::Busy),
                ),
                Span::styled(
                    if state.chat.draft.is_empty() {
                        if phase.is_empty() {
                            "still writing".to_owned()
                        } else {
                            phase
                        }
                    } else {
                        "writing · checking sources".to_owned()
                    },
                    theme.meta(),
                ),
            ]));
        }
        (answer, true)
    };
    // The line count is only exact for the answer, which arrives pre-wrapped.
    // Placeholder and error text is wrapped at render time, so its logical line
    // count would understate the real height and produce a wrong marker.
    let shown = areas.answer.height;
    let hidden = if already_wrapped {
        (text.lines.len() as u16).saturating_sub(state.chat_scroll.saturating_add(shown))
    } else {
        0
    };
    // Reserve the last row for the marker rather than drawing over content.
    let body = if hidden > 0 && shown > 1 {
        Rect::new(
            areas.answer.x,
            areas.answer.y,
            areas.answer.width,
            shown.saturating_sub(1),
        )
    } else {
        areas.answer
    };
    let paragraph = Paragraph::new(text).scroll((state.chat_scroll, 0));
    frame.render_widget(
        if already_wrapped {
            paragraph
        } else {
            paragraph.wrap(Wrap { trim: false })
        },
        body,
    );
    if hidden > 0 && shown > 1 {
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled("↓ ", theme.key()),
                Span::styled(format!("{hidden} more lines"), theme.meta()),
            ])),
            Rect::new(
                areas.answer.x,
                areas.answer.bottom().saturating_sub(1),
                areas.answer.width,
                1,
            ),
        );
    }

    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled("Ask across ", theme.meta()),
            Span::styled(
                truncate(
                    &state.chat.scope_title,
                    areas.scope.width.saturating_sub(14) as usize,
                ),
                theme.body(),
            ),
        ])),
        areas.scope,
    );
    let prompt = Rect::new(
        areas.composer.x,
        areas.composer.y,
        CHAT_PROMPT_WIDTH,
        areas.composer.height,
    );
    frame.render_widget(
        Paragraph::new(Line::styled(
            "›",
            if editing {
                Style::default().fg(theme.focus)
            } else {
                theme.meta()
            },
        )),
        prompt,
    );
    render_inline_editor(
        frame,
        areas.editor,
        &state.chat.question,
        "",
        editing,
        theme,
    );
}

/// Breaks text to `width` on word boundaries, falling back to a hard break for
/// a word longer than the measure.
fn wrap_plain(text: &str, width: u16) -> Vec<String> {
    let width = width.max(1) as usize;
    let mut lines = Vec::new();
    for paragraph in text.split('\n') {
        let mut current = String::new();
        for word in paragraph.split_whitespace() {
            if word.chars().count() > width {
                if !current.is_empty() {
                    lines.push(std::mem::take(&mut current));
                }
                let mut chunk = String::new();
                for character in word.chars() {
                    if chunk.chars().count() == width {
                        lines.push(std::mem::take(&mut chunk));
                    }
                    chunk.push(character);
                }
                current = chunk;
                continue;
            }
            let extra = if current.is_empty() { 0 } else { 1 };
            if current.chars().count() + extra + word.chars().count() > width {
                lines.push(std::mem::take(&mut current));
            } else if extra == 1 {
                current.push(' ');
            }
            current.push_str(word);
        }
        lines.push(current);
    }
    lines
}

/// What fills the conversation before there is an answer: onboarding for an
/// empty library, live phase while a run is in flight, otherwise an invitation.
fn chat_placeholder_lines(
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) -> Vec<Line<'static>> {
    if state.chat.request_pending || state.chat.active_run.is_some() {
        let phase = chat_phase_display(state);
        return vec![Line::from(vec![
            Span::styled(
                format!("{} ", spinner(metrics.animation_tick)),
                theme.status(StatusLevel::Busy),
            ),
            Span::styled(
                if phase.is_empty() {
                    "Searching your library".to_owned()
                } else {
                    phase
                },
                theme.body(),
            ),
        ])];
    }
    if state.documents.is_empty() {
        return vec![
            Line::styled("No books yet", theme.title()),
            Line::from(""),
            Line::from(vec![
                Span::styled("I", theme.key()),
                Span::styled("  add a PDF or a folder of PDFs", theme.body()),
            ]),
            Line::from(vec![
                Span::styled("Ctrl+H", theme.key()),
                Span::styled("  check the model preset", theme.body()),
            ]),
            Line::from(""),
            Line::styled(
                "Books are indexed on this machine and answers cite the page they came from.",
                theme.meta(),
            ),
        ];
    }
    vec![
        Line::styled(
            format!("Ask a question across {}.", state.chat.scope_title),
            theme.body(),
        ),
        Line::from(""),
        Line::styled(
            "Answers cite the book and page they came from; the evidence opens on the right.",
            theme.meta(),
        ),
    ]
}

fn chat_phase_display(state: &AppState) -> String {
    if state.chat.phase == "waiting"
        && let Some(detail) = state
            .jobs
            .values()
            .find(|job| !is_terminal(&job.status))
            .and_then(|job| job.progress_detail.as_ref())
        && let (Some(start), Some(end)) = (detail.page_start, detail.page_end)
    {
        return format!("Waiting · indexing pages {start}–{end}");
    }
    state.chat.phase_label.clone()
}

fn render_books_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let focused = state.focus_pane == FocusPane::Workspace;
    let [summary, list] =
        Layout::vertical([Constraint::Length(2), Constraint::Fill(1)]).areas(area);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(format!("{} books", state.documents.len()), theme.body()),
            Span::styled(
                format!(
                    "   {}   {}",
                    state.library.filter.label(),
                    state.library.sort.label()
                ),
                theme.meta(),
            ),
        ])),
        summary,
    );
    let documents = library_documents(state);
    let items = if documents.is_empty() {
        vec![ListItem::new(vec![
            Line::styled("No books indexed yet", theme.body()),
            Line::from(vec![
                Span::styled("I", theme.key()),
                Span::styled("  add a PDF or a folder", theme.meta()),
            ]),
        ])]
    } else {
        documents
            .iter()
            .map(|document| {
                let detail = state.library.details.get(&document.id);
                let pages = document
                    .page_count
                    .map_or("?".into(), |value| value.to_string());
                // Only the facts that vary earn a column: a "ready" status and
                // an unknown size are noise on every row.
                let mut meta = format!("  {pages} pages");
                if !document.status.eq_ignore_ascii_case("ready") {
                    meta.push_str(&format!(" · {}", document.status));
                }
                if let Some(size) = detail.map(|item| item.size_bytes).filter(|size| *size > 0) {
                    meta.push_str(&format!(" · {}", format_bytes(size)));
                }
                ListItem::new(vec![
                    Line::from(vec![
                        Span::styled(ico(state, Icon::Book), theme.meta()),
                        Span::styled(document.title.clone(), theme.body()),
                    ]),
                    Line::styled(meta, theme.meta()),
                ])
            })
            .collect()
    };
    let mut list_state = ListState::default();
    if !documents.is_empty() {
        list_state.select(Some(
            state.asset_cursor.min(documents.len().saturating_sub(1)),
        ));
    }
    frame.render_stateful_widget(selection_list(items, focused, theme), list, &mut list_state);
}

/// Rows above the job list. Shared with hit-testing.
pub(crate) const INDEXING_INTRO_HEIGHT: u16 = 2;

fn render_indexing_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    _metrics: &RuntimeMetrics,
) {
    let focused = state.focus_pane == FocusPane::Workspace;
    let [intro, jobs] = Layout::vertical([Constraint::Length(2), Constraint::Fill(1)]).areas(area);
    let active = state
        .jobs
        .values()
        .filter(|job| !is_terminal(&job.status))
        .count();
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(
                    if active > 0 {
                        format!("{} running", active)
                    } else {
                        "idle".to_owned()
                    },
                    theme.status(if active > 0 {
                        StatusLevel::Busy
                    } else {
                        StatusLevel::Ok
                    }),
                ),
                Span::styled(format!("   {} runs", state.jobs.len()), theme.meta()),
            ]),
            pipeline_strip(state, intro.width, theme),
        ]),
        intro,
    );
    let items = if state.jobs.is_empty() {
        vec![ListItem::new(vec![
            Line::styled("No indexing runs yet", theme.body()),
            Line::from(vec![
                Span::styled("I", theme.key()),
                Span::styled("  add a PDF or a folder", theme.meta()),
            ]),
        ])]
    } else {
        state
            .jobs
            .values()
            .map(|job| activity_item(job, jobs.width, state.icon_set, theme))
            .collect()
    };
    let mut list_state = ListState::default();
    list_state.select(
        (!state.jobs.is_empty())
            .then_some(state.job_cursor.min(state.jobs.len().saturating_sub(1))),
    );
    frame.render_stateful_widget(selection_list(items, focused, theme), jobs, &mut list_state);
}

fn render_history_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let focused = state.focus_pane == FocusPane::Workspace;
    let sessions = state
        .active_workspace
        .as_ref()
        .and_then(|id| state.chat_sessions.get(id))
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let items = if sessions.is_empty() {
        vec![ListItem::new(Line::styled(
            "No saved conversations",
            Style::default().fg(theme.muted),
        ))]
    } else {
        sessions
            .iter()
            .map(|session| {
                ListItem::new(vec![
                    Line::styled(
                        truncate(&session.question, area.width.saturating_sub(4) as usize),
                        theme.title(),
                    ),
                    Line::styled(
                        format!(
                            "{} · {} citations",
                            session.created_at,
                            session.citations.len()
                        ),
                        Style::default().fg(theme.muted),
                    ),
                ])
            })
            .collect()
    };
    let mut list_state = ListState::default();
    list_state.select(
        (!sessions.is_empty())
            .then_some(state.history_cursor.min(sessions.len().saturating_sub(1))),
    );
    frame.render_stateful_widget(selection_list(items, focused, theme), area, &mut list_state);
}

fn render_retrieval_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let focused = state.focus_pane == FocusPane::Workspace;
    let [search, results] =
        Layout::vertical([Constraint::Length(3), Constraint::Fill(1)]).areas(area);
    render_inline_editor(
        frame,
        search,
        &state.search.query,
        "Search chunks · Enter run",
        state.focus_pane == FocusPane::Workspace && state.input_mode == InputMode::Text,
        theme,
    );
    let items = if state.search.results.is_empty() {
        vec![ListItem::new(Line::styled(
            "Run a retrieval query to inspect ranking.",
            Style::default().fg(theme.muted),
        ))]
    } else {
        state
            .search
            .results
            .iter()
            .map(|hit| {
                ListItem::new(vec![
                    Line::from(vec![
                        Span::styled(
                            format!("{:.3}  ", hit.score.unwrap_or_default()),
                            Style::default()
                                .fg(theme.green)
                                .add_modifier(Modifier::BOLD),
                        ),
                        Span::styled(
                            format!("pages {:?}", hit.pages),
                            Style::default().fg(theme.cyan),
                        ),
                    ]),
                    Line::styled(
                        truncate(
                            &hit.content.replace('\n', " "),
                            area.width.saturating_sub(3) as usize,
                        ),
                        Style::default().fg(theme.text),
                    ),
                ])
            })
            .collect()
    };
    let mut list_state = ListState::default();
    list_state.select(
        (!state.search.results.is_empty()).then_some(
            state
                .search
                .cursor
                .min(state.search.results.len().saturating_sub(1)),
        ),
    );
    frame.render_stateful_widget(
        selection_list(items, focused, theme),
        results,
        &mut list_state,
    );
}

fn render_sources_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let focused = state.focus_pane == FocusPane::Workspace;
    let [list, editor] = Layout::vertical([Constraint::Fill(1), Constraint::Length(3)]).areas(area);
    let items = if state.sources.is_empty() {
        vec![ListItem::new(Line::styled(
            "No reusable sources",
            Style::default().fg(theme.muted),
        ))]
    } else {
        state
            .sources
            .iter()
            .map(|source| {
                ListItem::new(vec![
                    Line::from(vec![
                        Span::styled(
                            if source.enabled { "● " } else { "○ " },
                            Style::default().fg(if source.enabled {
                                theme.green
                            } else {
                                theme.muted
                            }),
                        ),
                        Span::styled(&source.name, theme.title()),
                        Span::styled(
                            format!("  {}", source.source_type),
                            Style::default().fg(theme.cyan),
                        ),
                    ]),
                    Line::styled(
                        truncate(&source.location, area.width.saturating_sub(4) as usize),
                        Style::default().fg(theme.muted),
                    ),
                ])
            })
            .collect()
    };
    let mut list_state = ListState::default();
    list_state.select(
        (!state.sources.is_empty()).then_some(
            state
                .source_cursor
                .min(state.sources.len().saturating_sub(1)),
        ),
    );
    frame.render_stateful_widget(selection_list(items, focused, theme), list, &mut list_state);
    render_inline_editor(
        frame,
        editor,
        &state.source_location,
        "Add file, directory, or URL",
        state.input_mode == InputMode::Text,
        theme,
    );
}

fn render_quality_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let lines = state.quality.as_ref().map_or_else(
        || {
            vec![Line::styled(
                "No quality report loaded.",
                Style::default().fg(theme.muted),
            )]
        },
        |quality| {
            let mut lines = vec![
                Line::styled(
                    quality.status.clone(),
                    Style::default()
                        .fg(if quality.failed_jobs == 0 {
                            theme.green
                        } else {
                            theme.yellow
                        })
                        .add_modifier(Modifier::BOLD),
                ),
                Line::from(""),
                Line::from(format!("Documents          {}", quality.document_count)),
                Line::from(format!("Completed imports  {}", quality.completed_imports)),
                Line::from(format!("Failed jobs        {}", quality.failed_jobs)),
                Line::from(""),
                Line::styled("Issues", theme.section()),
            ];
            if quality.issues.is_empty() {
                lines.push(Line::styled(
                    "✓ No issues detected",
                    Style::default().fg(theme.green),
                ));
            } else {
                lines.extend(quality.issues.iter().map(|issue| {
                    Line::styled(format!("! {issue}"), Style::default().fg(theme.yellow))
                }));
            }
            lines
        },
    );
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), area);
}

fn render_backups_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let focused = state.focus_pane == FocusPane::Workspace;
    let items = if state.backups.is_empty() {
        vec![ListItem::new(Line::styled(
            "No backups yet · press B to create one",
            Style::default().fg(theme.muted),
        ))]
    } else {
        state
            .backups
            .iter()
            .map(|backup| {
                ListItem::new(vec![
                    Line::from(vec![
                        Span::styled(
                            if backup.verified { "✓ " } else { "! " },
                            Style::default().fg(if backup.verified {
                                theme.green
                            } else {
                                theme.yellow
                            }),
                        ),
                        Span::styled(&backup.id, theme.title()),
                        Span::styled(
                            format!("  {}", format_bytes(backup.size_bytes)),
                            Style::default().fg(theme.cyan),
                        ),
                    ]),
                    Line::styled(&backup.created_at, Style::default().fg(theme.muted)),
                ])
            })
            .collect()
    };
    let mut list_state = ListState::default();
    list_state.select(
        (!state.backups.is_empty()).then_some(
            state
                .backup_cursor
                .min(state.backups.len().saturating_sub(1)),
        ),
    );
    frame.render_stateful_widget(selection_list(items, focused, theme), area, &mut list_state);
}

fn render_filter_chip(
    frame: &mut Frame<'_>,
    area: Rect,
    label: &str,
    value: &str,
    accent: Color,
    theme: &Theme,
) {
    // The chips sit side by side, so the value has to fit its own share of the
    // row or it runs into the next chip.
    // label + two spaces + " ›" + one column of air before the next chip.
    let budget = (area.width as usize).saturating_sub(label.chars().count() + 5);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                label.to_owned(),
                Style::default().fg(accent).add_modifier(Modifier::BOLD),
            ),
            Span::raw("  "),
            Span::styled(truncate(value, budget), Style::default().fg(theme.text)),
            Span::styled(" ›", theme.meta()),
        ])),
        area,
    );
}

fn model_transfer_line(
    state: &AppState,
    metrics: &RuntimeMetrics,
    theme: &Theme,
    width: u16,
) -> Line<'static> {
    if state.model_manager.busy {
        let spans = vec![
            Span::styled(
                format!("{} ", spinner(metrics.animation_tick)),
                Style::default().fg(theme.yellow),
            ),
            Span::styled(
                truncate(
                    &state.model_manager.transfer_status,
                    width.saturating_sub(20) as usize,
                ),
                Style::default().fg(theme.text),
            ),
        ];
        Line::from(spans)
    } else {
        Line::from(vec![
            Span::styled("● ", theme.status(StatusLevel::Ok)),
            Span::styled(
                if state.model_manager.transfer_status.is_empty() {
                    "Ready".to_owned()
                } else {
                    state.model_manager.transfer_status.clone()
                },
                theme.meta(),
            ),
        ])
    }
}

fn render_model_transfer_status(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    metrics: &RuntimeMetrics,
    theme: &Theme,
) {
    if state.model_manager.busy && state.model_manager.transfer_total > 0 {
        let [message, progress] =
            Layout::vertical([Constraint::Length(1), Constraint::Length(1)]).areas(area);
        frame.render_widget(
            Paragraph::new(model_transfer_line(state, metrics, theme, message.width)),
            message,
        );
        let ratio = (state.model_manager.transfer_completed as f64
            / state.model_manager.transfer_total as f64)
            .clamp(0.0, 1.0);
        frame.render_widget(
            LineGauge::default()
                .ratio(ratio)
                .label(format!(
                    "Download {:>3.0}% · {} / {}",
                    ratio * 100.0,
                    human_memory(state.model_manager.transfer_completed),
                    human_memory(state.model_manager.transfer_total),
                ))
                .filled_style(Style::default().fg(theme.green))
                .unfilled_style(Style::default().fg(theme.border)),
            progress,
        );
    } else {
        frame.render_widget(
            Paragraph::new(model_transfer_line(state, metrics, theme, area.width)),
            area,
        );
    }
}

fn render_foundry_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    hardware: &HardwareProfileResponse,
) {
    let focused = state.focus_pane == FocusPane::Workspace;
    let [summary, rail, packages, status] = foundry_setup_areas(area);
    let controls = foundry_controls(state);
    let [preset_list, controls_area] = model_center_areas(packages, controls.len());
    let profile = performance_profile(state.model_manager.profile).label();
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled("Local models", theme.title()),
                Span::styled(
                    format!(
                        "  Tier {} · {} · {} · {}K",
                        hardware.tier,
                        profile,
                        state.model_manager.quantization.label(),
                        state.model_manager.context_tokens / 1024
                    ),
                    Style::default().fg(theme.text),
                ),
            ]),
            Line::styled(
                format!(
                    "Limited by {} · catalog {} · Expert: Models → custom controls",
                    nonempty(&hardware.limiting_factor, "hardware"),
                    nonempty(&hardware.catalog_version, "legacy")
                ),
                Style::default().fg(theme.muted),
            ),
        ]),
        summary,
    );

    let configured = configured_models(state);
    let selected_package = state
        .model_manager
        .packages
        .get(state.model_manager.package_cursor);
    let mut rail_spans = Vec::new();
    for (index, (role, name)) in configured.iter().enumerate() {
        let loaded = metrics
            .loaded_models
            .iter()
            .any(|model| model_matches(&model.name, name));
        let configured = name != "not configured";
        let recommended = selected_package.is_some_and(|package| {
            package
                .models
                .iter()
                .any(|model| model.role.label() == role)
        });
        if index > 0 {
            rail_spans.push(Span::styled(" ─ ", Style::default().fg(theme.border)));
        }
        rail_spans.push(Span::styled(
            if loaded {
                "● "
            } else if configured {
                "◆ "
            } else if recommended {
                "◇ "
            } else {
                "○ "
            },
            Style::default().fg(if loaded {
                theme.green
            } else if configured {
                theme.cyan
            } else if recommended {
                theme.orange
            } else {
                theme.muted
            }),
        ));
        rail_spans.push(Span::styled(
            role.clone(),
            if configured {
                theme.body()
            } else {
                theme.meta()
            },
        ));
    }
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(rail_spans),
            Line::styled(
                format!("{} loaded", metrics.loaded_models.len()),
                Style::default().fg(theme.muted),
            ),
        ]),
        section_inner(rail),
    );
    render_section_label(frame, rail, "Roles", theme);

    let items = if state.model_manager.packages.is_empty() {
        vec![ListItem::new(vec![
            Line::styled(
                if state.model_manager.busy {
                    "Finding the best local setup…"
                } else {
                    "No recommendations loaded. Press R to scan the catalog."
                },
                Style::default().fg(theme.muted),
            ),
            Line::styled(
                "Recommendations use available memory, context and quantization.",
                Style::default().fg(theme.muted),
            ),
        ])]
    } else {
        state
            .model_manager
            .packages
            .iter()
            .map(|package| {
                let fit_color = match package.fit {
                    ModelFit::Comfortable => theme.green,
                    ModelFit::Tight => theme.yellow,
                };
                ListItem::new(vec![
                    Line::from(vec![
                        Span::styled(
                            format!("#{} ", package.recommended_rank),
                            Style::default()
                                .fg(theme.orange)
                                .add_modifier(Modifier::BOLD),
                        ),
                        Span::styled(
                            preset_name(package.recommended_rank).to_owned(),
                            theme.title(),
                        ),
                        Span::styled(
                            format!(
                                "  {} · {}",
                                package.fit.label(),
                                human_memory(package.total_estimated_memory)
                            ),
                            Style::default().fg(fit_color),
                        ),
                    ]),
                    Line::styled(
                        format!("   {}", package.summary),
                        Style::default().fg(theme.muted),
                    ),
                ])
            })
            .collect()
    };
    let mut list_state = ListState::default();
    list_state.select(
        (!state.model_manager.packages.is_empty()).then_some(
            state
                .model_manager
                .package_cursor
                .min(state.model_manager.packages.len().saturating_sub(1)),
        ),
    );
    render_section_label(frame, preset_list, "Recommended for this device", theme);
    frame.render_stateful_widget(
        selection_list(items, focused, theme),
        section_inner(preset_list),
        &mut list_state,
    );
    render_model_center_controls(frame, controls_area, state, theme, &controls);
    render_model_transfer_status(frame, status, state, metrics, theme);
}

fn render_models_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    let focused = state.focus_pane == FocusPane::Workspace;
    let [filters, search, list, status] = foundry_catalog_areas(area);
    let controls = foundry_controls(state);
    let [model_list, controls_area] = model_center_areas(list, controls.len());
    let [source, role, count] = catalog_filter_areas(filters);
    render_filter_chip(
        frame,
        source,
        "S Source",
        state.model_manager.source.label(),
        theme.focus,
        theme,
    );
    render_filter_chip(
        frame,
        role,
        "[ ] Role",
        state.model_manager.category.label(),
        theme.purple,
        theme,
    );
    frame.render_widget(
        Paragraph::new(Line::styled(
            format!("{} compatible", state.model_manager.compatible),
            Style::default().fg(theme.muted),
        ))
        .alignment(Alignment::Right),
        count,
    );
    render_inline_editor(
        frame,
        search,
        &state.model_manager.query,
        "/ search catalog",
        state.model_manager.searching,
        theme,
    );
    let items = if state.model_manager.entries.is_empty() {
        vec![ListItem::new(Line::styled(
            if state.model_manager.busy {
                "Loading model catalog…"
            } else {
                "Press R to load the model catalog."
            },
            Style::default().fg(theme.muted),
        ))]
    } else {
        state
            .model_manager
            .entries
            .iter()
            .map(|entry| {
                let loaded = metrics
                    .loaded_models
                    .iter()
                    .any(|model| model_matches(&model.name, &entry.id));
                ListItem::new(Line::from(vec![
                    Span::styled(
                        if loaded {
                            "● "
                        } else if entry.installed {
                            "◆ "
                        } else {
                            "○ "
                        },
                        Style::default().fg(if loaded {
                            theme.green
                        } else if entry.installed {
                            theme.cyan
                        } else {
                            theme.muted
                        }),
                    ),
                    Span::styled(
                        truncate(&entry.id, model_list.width.saturating_sub(30) as usize),
                        theme.title(),
                    ),
                    Span::styled(
                        format!(
                            "  {} · {}{}",
                            entry.fit.label(),
                            human_memory(entry.estimated_memory),
                            entry
                                .recommended_rank
                                .map_or(String::new(), |rank| format!(" · #{rank}"))
                        ),
                        Style::default().fg(match entry.fit {
                            ModelFit::Comfortable => theme.green,
                            ModelFit::Tight => theme.yellow,
                        }),
                    ),
                ]))
            })
            .collect()
    };
    let mut list_state = ListState::default();
    list_state.select(
        (!state.model_manager.entries.is_empty()).then_some(
            state
                .model_manager
                .cursor
                .min(state.model_manager.entries.len().saturating_sub(1)),
        ),
    );
    frame.render_stateful_widget(
        selection_list(items, focused, theme),
        model_list,
        &mut list_state,
    );
    render_model_center_controls(frame, controls_area, state, theme, &controls);
    let mut status_lines = vec![model_transfer_line(state, metrics, theme, status.width)];
    if state.model_manager.truncated {
        status_lines.push(Line::styled(
            "Hub scan capped · search scans matching repositories",
            Style::default().fg(theme.orange),
        ));
    }
    if state.model_manager.busy && state.model_manager.transfer_total > 0 {
        render_model_transfer_status(frame, status, state, metrics, theme);
    } else {
        frame.render_widget(Paragraph::new(status_lines), status);
    }
}

pub(crate) fn model_center_areas(area: Rect, control_count: usize) -> [Rect; 2] {
    let maximum = area.height.saturating_sub(3);
    let controls = (control_count as u16 + 2).min(maximum);
    Layout::vertical([Constraint::Fill(1), Constraint::Length(controls)]).areas(area)
}

fn render_model_center_controls(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    controls: &[FoundryControl],
) {
    if area.height == 0 {
        return;
    }
    let lines = controls
        .iter()
        .enumerate()
        .map(|(index, control)| {
            foundry_control_line(
                *control,
                state,
                state.model_manager.center_controls_active
                    && state.model_manager.center_control_cursor == index,
                theme,
            )
        })
        .collect::<Vec<_>>();
    render_section_label(frame, area, "Setup", theme);
    frame.render_widget(Paragraph::new(lines), section_inner(area));
}

fn preset_name(rank: u8) -> &'static str {
    match rank {
        1 => "Fast",
        2 => "Balanced",
        _ => "Quality",
    }
}

fn render_system_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    let backend = state
        .backend
        .as_ref()
        .map_or("not connected".into(), |meta| {
            format!("{} · API {}", meta.backend_id, meta.api_version)
        });
    let backend_version = state
        .backend
        .as_ref()
        .map_or("unknown", |meta| meta.omarag_version.as_str());
    let client_version = env!("CARGO_PKG_VERSION");
    let versions_match = backend_version == client_version;
    let [runtime, machine] =
        Layout::vertical([Constraint::Length(7), Constraint::Fill(1)]).areas(area);

    render_section_label(frame, runtime, "Runtime", theme);
    frame.render_widget(
        Paragraph::new(vec![
            field("Backend", backend, theme),
            field_styled(
                "Connection",
                state.connection.label(),
                theme.status(match state.connection {
                    ConnectionState::Connected => StatusLevel::Ok,
                    ConnectionState::Disconnected { .. } => StatusLevel::Error,
                    _ => StatusLevel::Busy,
                }),
                theme,
            ),
            field_styled(
                "Versions",
                format!("TUI {client_version} · API {backend_version}"),
                theme.status(if versions_match {
                    StatusLevel::Ok
                } else {
                    StatusLevel::Error
                }),
                theme,
            ),
            field(
                "Haiku RAG",
                state
                    .backend
                    .as_ref()
                    .and_then(|meta| meta.haiku_version.as_deref())
                    .unwrap_or("not installed"),
                theme,
            ),
            Line::styled(
                if versions_match {
                    "Components are compatible."
                } else {
                    "Version mismatch: restart the daemon after updating OmaRag."
                },
                if versions_match {
                    theme.meta()
                } else {
                    theme.status(StatusLevel::Error)
                },
            ),
        ])
        .wrap(Wrap { trim: false }),
        section_inner(runtime),
    );

    render_section_label(frame, machine, "This machine", theme);
    let inner = section_inner(machine);
    let bar_width = inner.width.saturating_sub(28).clamp(8, 24);
    let mut lines = vec![field(
        "CPU",
        format!("{} threads", metrics.cpu_count),
        theme,
    )];
    let mut load = vec![Span::styled(
        format!("{:<LABEL_COLUMN$}", "Load"),
        theme.meta(),
    )];
    load.extend(progress::bar(
        bar_width,
        f64::from(metrics.cpu_usage) / 100.0,
        theme,
    ));
    load.push(Span::styled(
        format!(" {:.0}%", metrics.cpu_usage),
        theme.body(),
    ));
    lines.push(Line::from(load));

    let mut memory = vec![Span::styled(
        format!("{:<LABEL_COLUMN$}", "Memory"),
        theme.meta(),
    )];
    let memory_ratio = if metrics.memory_total > 0 {
        metrics.memory_used as f64 / metrics.memory_total as f64
    } else {
        0.0
    };
    memory.extend(progress::bar(bar_width, memory_ratio, theme));
    memory.push(Span::styled(
        format!(
            " {} / {}",
            human_memory(metrics.memory_used),
            human_memory(metrics.memory_total)
        ),
        theme.body(),
    ));
    lines.push(Line::from(memory));

    if metrics.vram_total > 0 {
        let mut vram = vec![Span::styled(
            format!("{:<LABEL_COLUMN$}", "VRAM"),
            theme.meta(),
        )];
        vram.extend(progress::bar(
            bar_width,
            metrics.vram_used as f64 / metrics.vram_total as f64,
            theme,
        ));
        vram.push(Span::styled(
            format!(
                " {} / {}",
                human_memory(metrics.vram_used),
                human_memory(metrics.vram_total)
            ),
            theme.body(),
        ));
        lines.push(Line::from(vram));
    }
    lines.push(field("Theme", theme.name, theme));
    lines.push(Line::raw(""));
    lines.push(Line::styled(
        "All inference and retrieval services remain local.",
        theme.meta(),
    ));
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

fn render_settings_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let advanced = state.interaction_level == InteractionLevel::Workshop;
    let [intro, options, appearance, editor] = Layout::vertical([
        Constraint::Length(3),
        Constraint::Length(4),
        Constraint::Length(4),
        Constraint::Fill(1),
    ])
    .areas(area);

    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(
                    if state.config_dirty {
                        "Unsaved changes"
                    } else {
                        "Saved"
                    },
                    theme.status(if state.config_dirty {
                        StatusLevel::Busy
                    } else {
                        StatusLevel::Ok
                    }),
                ),
                Span::styled(
                    format!("   {} mode", state.interaction_level.label().to_lowercase()),
                    theme.meta(),
                ),
            ]),
            Line::styled(
                if advanced {
                    "Every workspace setting is editable below."
                } else {
                    "Safe defaults are active. Switch to Advanced to edit the full configuration."
                },
                theme.meta(),
            ),
        ])
        .wrap(Wrap { trim: false }),
        intro,
    );

    let explain_terms = !state.bold_term_explanations_disabled;
    render_section_label(frame, options, "Chat", theme);
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(
                    if explain_terms { "[✓] " } else { "[ ] " },
                    theme.status(if explain_terms {
                        StatusLevel::Ok
                    } else {
                        StatusLevel::Idle
                    }),
                ),
                Span::styled("Explain bold terms on click", theme.body()),
                Span::styled("   B", theme.key()),
            ]),
            Line::styled(
                "    A bold term can be clicked for a short definition.",
                theme.meta(),
            ),
        ])
        .wrap(Wrap { trim: false }),
        section_inner(options),
    );

    render_section_label(frame, appearance, "Appearance", theme);
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled("    Icons     ", theme.meta()),
                Span::styled(format!("{:<14}", state.icon_mode.label()), theme.body()),
                Span::styled("K", theme.key()),
                Span::styled("  cycle", theme.meta()),
            ]),
            Line::from(vec![
                Span::styled("    Glyphs    ", theme.meta()),
                Span::styled(format!("{:<14}", state.icon_set.label()), theme.body()),
                Span::styled("G", theme.key()),
                Span::styled(
                    "  cycle · pick ASCII if icons render as boxes",
                    theme.meta(),
                ),
            ]),
        ]),
        section_inner(appearance),
    );

    if advanced {
        render_section_label(frame, editor, "Workspace YAML", theme);
        render_inline_editor(
            frame,
            section_inner(editor),
            &state.config_editor,
            "",
            state.input_mode == InputMode::Text,
            theme,
        );
    } else {
        render_section_label(frame, editor, "Active defaults", theme);
        frame.render_widget(
            Paragraph::new(vec![
                Line::styled(
                    "Answers use the selected library and cite original pages.",
                    theme.body(),
                ),
                Line::styled(
                    "PDF processing preserves layout, tables, formulas and page anchors.",
                    theme.body(),
                ),
                Line::styled(
                    "Models stay local; unsupported claims are rejected in strict mode.",
                    theme.body(),
                ),
                Line::from(""),
                Line::from(vec![
                    Span::styled("M", theme.key()),
                    Span::styled("  switch to Advanced for every setting", theme.meta()),
                ]),
            ])
            .wrap(Wrap { trim: false }),
            section_inner(editor),
        );
    }
}

/// Rows above the palette list. Shared with hit-testing.
pub(crate) const THEME_INTRO_HEIGHT: u16 = 3;

/// The palette picker. One scrolling row per theme, because the bundled set is
/// far too long for a fixed grid: swatches, name, where it came from, and
/// whether it is a light or a dark ground.
fn render_themes_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let focused = state.focus_pane == FocusPane::Workspace;
    let [intro, list] =
        Layout::vertical([Constraint::Length(THEME_INTRO_HEIGHT), Constraint::Fill(1)]).areas(area);

    let active = Theme::at(state.theme_index);
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled("Active   ", theme.meta()),
                Span::styled(active.name, theme.body()),
                Span::styled(
                    format!("   {}   {}", active.source.label(), mode_label(active.mode)),
                    theme.meta(),
                ),
            ]),
            Line::styled(
                format!(
                    "{} palettes · Omarchy System follows your desktop",
                    Theme::count()
                ),
                theme.meta(),
            ),
        ]),
        intro,
    );

    let items = (0..Theme::count())
        .map(|index| {
            let palette = Theme::at(index);
            let applied = index == state.theme_index && state.theme_preview_origin.is_none();
            ListItem::new(Line::from(vec![
                // Three swatches show at a glance what the palette does with
                // accent, success and warning.
                Span::styled("  ", Style::default().bg(palette.focus)),
                Span::styled("  ", Style::default().bg(palette.green)),
                Span::styled("  ", Style::default().bg(palette.orange)),
                Span::raw(" "),
                Span::styled(
                    format!("{:<24}", truncate(palette.name, 24)),
                    if applied { theme.title() } else { theme.body() },
                ),
                Span::styled(format!("{:<10}", palette.source.label()), theme.meta()),
                Span::styled(mode_label(palette.mode), theme.meta()),
            ]))
        })
        .collect::<Vec<_>>();

    let mut list_state = ListState::default();
    list_state.select(Some(
        state.theme_cursor.min(Theme::count().saturating_sub(1)),
    ));
    frame.render_stateful_widget(selection_list(items, focused, theme), list, &mut list_state);
}

/// First visible palette row, mirroring how `List` scrolls its selection into
/// view. Shared with hit-testing so a click lands on the row that is drawn.
pub(crate) fn theme_list_scroll(state: &AppState, height: u16) -> usize {
    let height = height.max(1) as usize;
    let cursor = state.theme_cursor.min(Theme::count().saturating_sub(1));
    let last = Theme::count().saturating_sub(height);
    cursor.saturating_sub(height.saturating_sub(1)).min(last)
}

const fn mode_label(mode: ThemeMode) -> &'static str {
    match mode {
        ThemeMode::Dark => "dark",
        ThemeMode::Light => "light",
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FoundryControl {
    Profile,
    Quantization,
    Context,
    Memory,
    AutomaticStack,
    InstallStack,
    Download,
    Load,
    Unload,
    Delete,
    Custom,
    Refresh,
}

pub(crate) fn foundry_controls(state: &AppState) -> Vec<FoundryControl> {
    let mut controls = vec![
        FoundryControl::Profile,
        FoundryControl::Quantization,
        FoundryControl::Context,
        FoundryControl::Memory,
    ];
    match state.view {
        View::FoundryOverview => {
            if state.active_workspace.is_some() {
                controls.push(FoundryControl::AutomaticStack);
            }
            if !state.model_manager.packages.is_empty() {
                controls.push(FoundryControl::InstallStack);
            }
            controls.push(FoundryControl::Refresh);
        }
        View::Models => {
            if let Some(entry) = state.model_manager.entries.get(state.model_manager.cursor) {
                if entry.installed || entry.source == ModelSource::Installed {
                    controls.extend([FoundryControl::Load, FoundryControl::Unload]);
                    if entry.source == ModelSource::Installed {
                        controls.push(FoundryControl::Delete);
                    }
                } else {
                    controls.push(FoundryControl::Download);
                }
            }
            controls.push(FoundryControl::Custom);
            controls.push(FoundryControl::Refresh);
        }
        _ => {}
    }
    controls
}

fn foundry_control_line(
    control: FoundryControl,
    state: &AppState,
    selected: bool,
    theme: &Theme,
) -> Line<'static> {
    let (key, label, value, accent) = match control {
        FoundryControl::Profile => (
            "F",
            "Profile",
            performance_profile(state.model_manager.profile)
                .label()
                .to_owned(),
            theme.green,
        ),
        FoundryControl::Quantization => (
            "Q",
            "Quant",
            state.model_manager.quantization.label().to_owned(),
            theme.purple,
        ),
        FoundryControl::Context => (
            "C",
            "Context",
            format!("{}K", state.model_manager.context_tokens / 1024),
            theme.cyan,
        ),
        FoundryControl::Memory => (
            "P",
            "Memory",
            state.model_manager.memory_policy.label().to_owned(),
            theme.orange,
        ),
        FoundryControl::AutomaticStack => (
            "G",
            "Use automatic stack",
            performance_profile(state.model_manager.profile)
                .label()
                .to_owned(),
            theme.green,
        ),
        FoundryControl::InstallStack => {
            let installed = state
                .model_manager
                .packages
                .get(state.model_manager.package_cursor)
                .is_some_and(|package| package.models.iter().all(|model| model.installed));
            (
                "A",
                if installed {
                    "Activate package"
                } else {
                    "Install package"
                },
                if installed {
                    if state.active_workspace.is_some() {
                        "for current library"
                    } else {
                        "select a library first"
                    }
                } else {
                    "then activate after verification"
                }
                .to_owned(),
                if installed { theme.green } else { theme.orange },
            )
        }
        FoundryControl::Download
            if state
                .model_manager
                .entries
                .get(state.model_manager.cursor)
                .is_some_and(|entry| {
                    entry.category == omarag_app::ModelCategory::Rerank
                        && entry.source == ModelSource::HuggingFace
                }) =>
        {
            (
                "D",
                "Use reranker",
                "library default".to_owned(),
                theme.green,
            )
        }
        FoundryControl::Download => ("D", "Download", "selected model".to_owned(), theme.green),
        FoundryControl::Load => ("L", "Load", "temporarily".to_owned(), theme.cyan),
        FoundryControl::Unload => ("U", "Unload", "selected model".to_owned(), theme.yellow),
        FoundryControl::Delete => ("X", "Delete", "local model".to_owned(), theme.red),
        FoundryControl::Custom => ("+", "Add custom", "ID or GGUF".to_owned(), theme.purple),
        FoundryControl::Refresh => ("R", "Refresh", "catalog".to_owned(), theme.yellow),
    };
    let _ = accent;
    let base = if selected {
        theme.selection_style(true)
    } else {
        theme.body()
    };
    // Widest label in the set, so the value column never collides with it.
    const LABEL_WIDTH: usize = 20;
    Line::from(vec![
        Span::styled(if selected { "▌ " } else { "  " }, base.fg(theme.focus)),
        Span::styled(key.to_owned(), base.patch(theme.key())),
        Span::styled(
            format!(" {:<LABEL_WIDTH$}", truncate(label, LABEL_WIDTH)),
            base,
        ),
        Span::styled(
            value,
            base.fg(if selected { theme.text } else { theme.muted }),
        ),
        Span::styled(
            if matches!(
                control,
                FoundryControl::Profile
                    | FoundryControl::Quantization
                    | FoundryControl::Context
                    | FoundryControl::Memory
            ) {
                "  ‹ ›"
            } else {
                ""
            },
            base.fg(theme.muted),
        ),
    ])
}

fn render_foundry_inspector(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    hardware: &HardwareProfileResponse,
) {
    let mut detail_lines = match state.view {
        View::FoundryOverview => state
            .model_manager
            .packages
            .get(state.model_manager.package_cursor)
            .map_or_else(
                || {
                    vec![
                        Line::styled("No setup selected", theme.section()),
                        Line::from(""),
                        Line::styled(
                            "Press R to build recommendations for this device.",
                            Style::default().fg(theme.muted),
                        ),
                    ]
                },
                |package| package_details(package, theme),
            ),
        View::Models => state
            .model_manager
            .entries
            .get(state.model_manager.cursor)
            .map_or_else(
                || {
                    vec![
                        Line::styled("No model selected", theme.section()),
                        Line::from(""),
                        Line::styled(
                            "Press R to load the catalog or change the filters.",
                            Style::default().fg(theme.muted),
                        ),
                    ]
                },
                |entry| model_details(entry, state, metrics, theme).lines,
            ),
        _ => Vec::new(),
    };
    if state.view == View::FoundryOverview {
        let mut hardware_lines = vec![
            Line::styled(
                format!(
                    "Hardware tier {} · {}",
                    hardware.tier,
                    performance_profile(state.model_manager.profile)
                ),
                theme.heading(),
            ),
            Line::styled(
                format!(
                    "Limit: {} · catalog {}",
                    nonempty(&hardware.limiting_factor, "unknown"),
                    nonempty(&hardware.catalog_version, "legacy")
                ),
                Style::default().fg(theme.muted),
            ),
            Line::styled(
                "Fast / Normal / Quality stay adaptive. Open Models for expert tuning.",
                theme.meta(),
            ),
        ];
        if !hardware.recommendations.is_empty() {
            hardware_lines.push(Line::from(""));
            hardware_lines.push(Line::styled("Automatic stack", theme.section()));
            hardware_lines.extend(hardware.recommendations.iter().map(|recommendation| {
                Line::from(vec![
                    Span::styled(
                        format!("{:<11}", role_label(&recommendation.role)),
                        theme.meta(),
                    ),
                    Span::styled(
                        recommendation.model.clone(),
                        Style::default().fg(theme.text),
                    ),
                ])
            }));
        }
        hardware_lines.push(Line::from(""));
        detail_lines.splice(0..0, hardware_lines);
    }
    frame.render_widget(
        Paragraph::new(detail_lines)
            .wrap(Wrap { trim: false })
            .scroll((state.inspector_scroll, 0)),
        area,
    );
}

struct InspectorRenderContext<'a> {
    previews: &'a mut [ChatImagePreview],
    media_previews: &'a mut [MediaImagePreview],
    visual: &'a VisualInspectorState,
    hardware: &'a HardwareProfileResponse,
    compact_shell: bool,
}

fn render_inspector(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    runtime: &mut InspectorRenderContext<'_>,
) {
    let title = match state.view {
        View::Conversation | View::Retrieval => "Evidence",
        View::Books => "Book details",
        View::Indexing | View::Activity => "Run details",
        View::FoundryOverview => "Stack details",
        View::Models => "Model details",
        View::System => "Runtime details",
        View::Settings => "Configuration",
        View::Themes => "Palette details",
        _ => "Details",
    };
    let focused = state.focus_pane == FocusPane::Inspector;
    render_frame(
        frame,
        area,
        title,
        &inspector_status_items(state),
        focused,
        theme.footer,
    );
    let inner = pane_inner(area);
    if matches!(state.view, View::FoundryOverview | View::Models) {
        render_foundry_inspector(frame, inner, state, theme, metrics, runtime.hardware);
        return;
    }
    if matches!(state.view, View::Conversation | View::Retrieval) {
        render_source_inspector(frame, inner, state, theme, metrics, runtime);
        return;
    }
    let lines = inspector_lines(state, theme, metrics, inner.width);
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .scroll((state.inspector_scroll, 0)),
        inner,
    );
}

fn render_source_inspector(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    runtime: &mut InspectorRenderContext<'_>,
) {
    let visual = runtime.visual;
    let media_count = visual
        .evidence
        .media
        .iter()
        .filter(|asset| asset.is_individual_asset())
        .take(VisualEvidenceResponse::MAX_MEDIA)
        .count();
    let focused = state.focus_pane == FocusPane::Inspector;
    let layout = visual_inspector_areas(
        area,
        runtime.compact_shell,
        visual.sources_collapsed,
        media_count,
    );
    if runtime.compact_shell {
        let selected = VisualInspectorTab::ALL
            .iter()
            .position(|tab| *tab == visual.tab)
            .unwrap_or_default();
        frame.render_widget(
            Tabs::new(VisualInspectorTab::ALL.map(|tab| tab.label()))
                .select(selected)
                .style(Style::default().fg(theme.muted))
                .highlight_style(theme.heading())
                .divider(" · "),
            layout.tabs,
        );
        match visual.tab {
            VisualInspectorTab::Pages => render_page_evidence(
                frame,
                layout.pages,
                state,
                theme,
                runtime.previews,
                &visual.evidence,
                focused,
            ),
            VisualInspectorTab::Figures => render_media_evidence(
                frame,
                layout.figures,
                theme,
                runtime.media_previews,
                visual,
                focused,
            ),
            VisualInspectorTab::Sources => {
                render_source_list(frame, layout.sources, state, theme, metrics, false)
            }
        }
        return;
    }

    render_page_evidence(
        frame,
        layout.pages,
        state,
        theme,
        runtime.previews,
        &visual.evidence,
        focused,
    );
    render_media_evidence(
        frame,
        layout.figures,
        theme,
        runtime.media_previews,
        visual,
        focused,
    );
    render_source_list(
        frame,
        layout.sources,
        state,
        theme,
        metrics,
        visual.sources_collapsed,
    );
}

/// Cited pages, as a labelled section of preview tiles. No borders: the label
/// names the group, the caption names each tile, and the selected tile is the
/// only one carrying the accent.
fn render_page_evidence(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    previews: &mut [ChatImagePreview],
    evidence: &VisualEvidenceResponse,
    focused: bool,
) {
    if area.height == 0 {
        return;
    }
    let page_refs = related_page_refs(state, Some(evidence));
    // Show the page of tiles the selection sits on, so evidence past the fourth
    // item is reachable instead of invisible.
    let selected_slot = page_refs
        .iter()
        .position(|(citation, page, _)| {
            *citation == state.citation_cursor && *page == state.citation_page_cursor
        })
        .unwrap_or(0);
    let start = evidence_page_start(selected_slot);
    let visible = page_refs.len().saturating_sub(start).min(EVIDENCE_PAGE);
    let label = if page_refs.len() > EVIDENCE_PAGE {
        format!(
            "Pages  {}–{} of {}",
            start + 1,
            start + visible,
            page_refs.len()
        )
    } else {
        count_label("Pages", page_refs.len())
    };
    render_section_label(frame, area, &label, theme);
    let inner = section_inner(area);
    if inner.height == 0 {
        return;
    }
    if page_refs.is_empty() {
        frame.render_widget(
            Paragraph::new("Ask a question to see the cited pages.")
                .style(theme.meta())
                .wrap(Wrap { trim: true }),
            inner,
        );
        return;
    }
    for (offset, tile) in evidence_tiles(inner, visible).into_iter().enumerate() {
        let slot = start + offset;
        let (citation_index, page_index, page) = page_refs[slot];
        let selected =
            citation_index == state.citation_cursor && page_index == state.citation_page_cursor;
        let source = state.chat.citations[citation_index]
            .document_title
            .as_deref()
            .unwrap_or("Source");
        render_tile(
            frame,
            tile,
            &format!("p.{page}"),
            source,
            selected,
            focused,
            theme,
            |frame, body| {
                if let Some(preview) = previews.iter_mut().find(|preview| {
                    preview.citation_index == citation_index && preview.page_index == page_index
                }) {
                    preview.receive_resizes();
                    frame.render_stateful_widget(
                        StatefulImage::default(),
                        body,
                        &mut preview.protocol,
                    );
                    true
                } else {
                    false
                }
            },
        );
    }
}

/// Extracted figures and tables. Absent evidence stays a single quiet line
/// rather than an empty box holding open a third of the column.
fn render_media_evidence(
    frame: &mut Frame<'_>,
    area: Rect,
    theme: &Theme,
    previews: &mut [MediaImagePreview],
    visual: &VisualInspectorState,
    focused: bool,
) {
    if area.height == 0 {
        return;
    }
    let media = visual
        .evidence
        .media
        .iter()
        .filter(|asset| asset.is_individual_asset())
        .take(VisualEvidenceResponse::MAX_MEDIA)
        .collect::<Vec<_>>();
    let media_start = evidence_page_start(visual.selected_media.min(media.len().saturating_sub(1)));
    let media_visible = media.len().saturating_sub(media_start).min(EVIDENCE_PAGE);
    let label = if media.len() > EVIDENCE_PAGE {
        format!(
            "Figures  {}–{} of {}",
            media_start + 1,
            media_start + media_visible,
            media.len()
        )
    } else {
        count_label("Figures", media.len())
    };
    render_section_label(frame, area, &label, theme);
    let inner = section_inner(area);
    if inner.height == 0 {
        return;
    }
    if media.is_empty() {
        // Kept short: the empty section is only one row tall, so a longer
        // sentence would be clipped mid-word in a narrow inspector.
        let message = if visual.legacy {
            "Figure extraction unavailable."
        } else {
            "No separate figure here."
        };
        frame.render_widget(
            Paragraph::new(message)
                .style(theme.meta())
                .wrap(Wrap { trim: true }),
            inner,
        );
        return;
    }
    for (offset, tile) in evidence_tiles(inner, media_visible).into_iter().enumerate() {
        let slot = media_start + offset;
        let asset = media[slot];
        let selected = slot == visual.selected_media;
        let label = asset.page.map_or_else(
            || media_kind_label(asset),
            |page| format!("{} · p.{page}", media_kind_label(asset)),
        );
        let caption = asset
            .caption
            .as_deref()
            .filter(|caption| !caption.trim().is_empty())
            .unwrap_or("Loading crop…");
        render_tile(
            frame,
            tile,
            &label,
            caption,
            selected,
            focused,
            theme,
            |frame, body| {
                if let Some(preview) = previews
                    .iter_mut()
                    .find(|preview| preview.media_id == asset.media_id)
                {
                    preview.receive_resizes();
                    frame.render_stateful_widget(
                        StatefulImage::default(),
                        body,
                        &mut preview.protocol,
                    );
                    true
                } else {
                    false
                }
            },
        );
    }
}

/// One evidence tile: a caption row and a body. `body` draws the image and
/// returns whether it managed to; otherwise the fallback text stands in.
#[allow(clippy::too_many_arguments)]
fn render_tile(
    frame: &mut Frame<'_>,
    area: Rect,
    label: &str,
    fallback: &str,
    selected: bool,
    focused: bool,
    theme: &Theme,
    body: impl FnOnce(&mut Frame<'_>, Rect) -> bool,
) {
    if area.height == 0 || area.width == 0 {
        return;
    }
    // A coloured caption alone was too weak to find at a glance; the selected
    // tile gets a left edge down its whole height.
    if selected {
        let edge = Style::default().fg(if focused {
            theme.workspace.border_active
        } else {
            theme.workspace.border
        });
        frame.render_widget(
            Paragraph::new(
                (0..area.height)
                    .map(|_| Line::styled("▏", edge))
                    .collect::<Vec<_>>(),
            ),
            Rect::new(area.x, area.y, 1, area.height),
        );
    }
    let marker = if selected {
        Theme::selection_marker(focused)
    } else {
        "  "
    };
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                marker,
                if selected {
                    Style::default().fg(theme.focus)
                } else {
                    theme.meta()
                },
            ),
            Span::styled(
                truncate(label, area.width.saturating_sub(3) as usize),
                if selected {
                    Style::default().fg(theme.focus)
                } else {
                    theme.meta()
                },
            ),
        ])),
        Rect::new(area.x, area.y, area.width, 1),
    );
    let inner = Rect::new(
        area.x.saturating_add(2),
        area.y.saturating_add(1),
        area.width.saturating_sub(3),
        area.height.saturating_sub(1),
    );
    if inner.height == 0 || inner.width == 0 {
        return;
    }
    if !body(frame, inner) {
        frame.render_widget(
            Paragraph::new(truncate(fallback, inner.width as usize * 3))
                .style(theme.meta())
                .wrap(Wrap { trim: true }),
            inner,
        );
    }
}

/// Human name for an extracted asset: `Figure`, `Table`, and so on.
fn media_kind_label(asset: &MediaEvidence) -> String {
    let kind = asset.kind.trim();
    if kind.is_empty() {
        "Figure".into()
    } else {
        let mut characters = kind.chars();
        characters
            .next()
            .map(|first| first.to_uppercase().collect::<String>() + characters.as_str())
            .unwrap_or_else(|| "Figure".into())
    }
}

/// `Pages 4`, or just `Pages` when there is nothing to count.
fn count_label(label: &str, count: usize) -> String {
    if count == 0 {
        label.to_owned()
    } else {
        format!("{label}  {count}")
    }
}

/// The answer's provenance and receipt.
fn render_source_list(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    collapsed: bool,
) {
    if area.height == 0 {
        return;
    }
    render_section_label(
        frame,
        area,
        if collapsed {
            "Sources  S expand"
        } else {
            "Sources"
        },
        theme,
    );
    if collapsed {
        return;
    }
    let inner = section_inner(area);
    if inner.height == 0 {
        return;
    }
    let lines = inspector_lines(state, theme, metrics, inner.width);
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .scroll((state.inspector_scroll, 0)),
        inner,
    );
}

/// How many evidence tiles fit on one page of the grid.
pub(crate) const EVIDENCE_PAGE: usize = 4;

/// First tile of the page holding `selected`, so evidence beyond the fourth item
/// is reachable rather than silently dropped.
pub(crate) fn evidence_page_start(selected: usize) -> usize {
    selected / EVIDENCE_PAGE * EVIDENCE_PAGE
}

pub(crate) fn evidence_tiles(area: Rect, count: usize) -> Vec<Rect> {
    match count {
        0 => Vec::new(),
        1 => vec![area],
        2 => Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)])
            .split(area)
            .to_vec(),
        _ => {
            let rows = Layout::vertical([Constraint::Percentage(50), Constraint::Percentage(50)])
                .split(area);
            rows.iter()
                .flat_map(|row| {
                    Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)])
                        .split(*row)
                        .to_vec()
                })
                .take(count.min(4))
                .collect()
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct VisualInspectorAreas {
    pub tabs: Rect,
    pub pages: Rect,
    pub figures: Rect,
    pub sources: Rect,
}

/// Splits the evidence column. Sections are sized by what they actually hold:
/// an empty figures section shrinks to its label instead of holding open a third
/// of the column, and the space goes to the pages and the receipt.
pub(crate) fn visual_inspector_areas(
    area: Rect,
    compact_shell: bool,
    sources_collapsed: bool,
    media_count: usize,
) -> VisualInspectorAreas {
    if compact_shell {
        let [tabs, content] =
            Layout::vertical([Constraint::Length(2), Constraint::Fill(1)]).areas(area);
        return VisualInspectorAreas {
            tabs,
            pages: content,
            figures: content,
            sources: content,
        };
    }
    let figures_constraint = if media_count == 0 {
        Constraint::Length(2)
    } else if media_count <= 2 {
        Constraint::Percentage(26)
    } else {
        Constraint::Percentage(34)
    };
    let source_constraint = if sources_collapsed {
        Constraint::Length(1)
    } else {
        Constraint::Percentage(34)
    };
    // A blank row before each label so the three groups read as separate
    // sections without needing a rule or a box between them.
    let [pages, _, figures, _, sources] = Layout::vertical([
        Constraint::Percentage(if media_count == 0 { 48 } else { 40 }),
        Constraint::Length(1),
        figures_constraint,
        Constraint::Length(1),
        source_constraint,
    ])
    .areas(area);
    let sources = Rect::new(
        sources.x,
        sources.y,
        sources.width,
        sources
            .height
            .saturating_add(area.bottom().saturating_sub(sources.bottom())),
    );
    VisualInspectorAreas {
        pages,
        figures,
        sources,
        ..VisualInspectorAreas::default()
    }
}

/// Legacy geometry retained for downstream input/tests. New code should use
/// `visual_inspector_areas` so pages, figures and sources never share a bucket.
pub(crate) fn source_inspector_areas(area: Rect) -> [Rect; 2] {
    let layout = visual_inspector_areas(area, false, false, 0);
    [
        Rect::new(
            layout.pages.x,
            layout.pages.y,
            layout.pages.width,
            layout.pages.height.saturating_add(layout.figures.height),
        ),
        layout.sources,
    ]
}

pub fn related_image_refs(state: &AppState) -> Vec<(usize, usize, u32)> {
    related_page_refs(state, None)
}

pub fn related_page_refs(
    state: &AppState,
    evidence: Option<&VisualEvidenceResponse>,
) -> Vec<(usize, usize, u32)> {
    let mut output = Vec::with_capacity(4);
    let mut seen = std::collections::BTreeSet::new();
    let mut consider = |citation_index: usize, page_index: usize| {
        let Some(citation) = state.chat.citations.get(citation_index) else {
            return;
        };
        let Some(page) = citation.pages.get(page_index).copied() else {
            return;
        };
        let source = citation
            .logical_document_id
            .as_deref()
            .or(citation.document_id.as_deref())
            .or(citation.document_title.as_deref())
            .unwrap_or("source");
        if output.len() < 4 && seen.insert((source.to_owned(), page)) {
            output.push((citation_index, page_index, page));
        }
    };
    if let Some(evidence) = evidence {
        for page_evidence in &evidence.pages {
            let citation_index = page_evidence
                .citation_index
                .filter(|index| *index < state.chat.citations.len())
                .or_else(|| {
                    state.chat.citations.iter().position(|citation| {
                        citation.pages.contains(&page_evidence.page)
                            && (page_evidence.document_id.is_none()
                                || citation.document_id == page_evidence.document_id
                                || citation.logical_document_id == page_evidence.document_id)
                    })
                });
            if let Some(citation_index) = citation_index
                && let Some(page_index) = state.chat.citations[citation_index]
                    .pages
                    .iter()
                    .position(|page| *page == page_evidence.page)
            {
                consider(citation_index, page_index);
            }
        }
    }
    for citation_index in 0..state.chat.citations.len() {
        consider(citation_index, 0);
    }
    for citation_index in 0..state.chat.citations.len() {
        for page_index in 1..state.chat.citations[citation_index].pages.len() {
            consider(citation_index, page_index);
        }
    }
    output
}

/// Row of the first citation inside `section_inner(sources)`.
///
/// `inspector_lines` opens the sources section with a count line and a blank
/// row, then two rows per citation. The run receipt now trails the citations
/// instead of preceding them, so it no longer shifts this offset — keep this in
/// step with `inspector_lines` or clicks select the wrong source.
pub(crate) const SOURCE_CITATION_ROW_OFFSET: u16 = 2;

/// Rows each citation occupies: a title line and a detail line.
pub(crate) const SOURCE_CITATION_ROW_HEIGHT: u16 = 2;

pub(crate) fn source_citation_row_offset(_state: &AppState) -> u16 {
    SOURCE_CITATION_ROW_OFFSET
}

fn inspector_lines(
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    width: u16,
) -> Vec<Line<'static>> {
    match state.view {
        View::Conversation | View::Retrieval => {
            if state.chat.citations.is_empty() {
                let mut lines = vec![Line::styled(
                    if state.chat.error.is_some() {
                        "The answer failed, so there are no sources."
                    } else if state.chat.receipt.is_some() {
                        "No usable source supported this answer."
                    } else {
                        "Ask a question. Sources and page anchors appear here."
                    },
                    Style::default().fg(theme.muted),
                )];
                lines.extend(trailing_receipt_lines(state, theme));
                return lines;
            }
            let mut lines = vec![
                Line::styled(
                    format!("{} cited", state.chat.citations.len()),
                    Style::default().fg(theme.muted),
                ),
                Line::from(""),
            ];
            for (index, citation) in state.chat.citations.iter().enumerate() {
                let selected = index == state.citation_cursor;
                let page_index = if selected {
                    state
                        .citation_page_cursor
                        .min(citation.pages.len().saturating_sub(1))
                } else {
                    0
                };
                let page = citation
                    .pages
                    .get(page_index)
                    .map_or("?".into(), |page| page.to_string());
                lines.push(Line::from(vec![
                    Span::styled(
                        format!(
                            "{}{} ",
                            if selected { "▌" } else { " " },
                            preferred_evidence_label(
                                citation.prompt_evidence_id.as_deref(),
                                citation.evidence_id.as_deref(),
                            )
                        ),
                        Style::default().fg(if selected { theme.focus } else { theme.yellow }),
                    ),
                    Span::styled(
                        truncate(
                            citation.document_title.as_deref().unwrap_or("Source"),
                            width.saturating_sub(6) as usize,
                        ),
                        Style::default()
                            .fg(if selected { theme.focus } else { theme.text })
                            .add_modifier(Modifier::BOLD),
                    ),
                ]));
                lines.push(Line::styled(
                    format!(
                        "    page {page} · {}/{} · {} anchors",
                        page_index.saturating_add(1),
                        citation.pages.len().max(1),
                        citation.primary_anchors.len(),
                    ),
                    Style::default().fg(theme.muted),
                ));
            }
            lines.extend(trailing_receipt_lines(state, theme));
            lines
        }
        View::Books => {
            let documents = library_documents(state);
            let Some(document) =
                documents.get(state.asset_cursor.min(documents.len().saturating_sub(1)))
            else {
                return vec![Line::styled(
                    "Select a book to inspect it.",
                    Style::default().fg(theme.muted),
                )];
            };
            let detail = state.library.details.get(&document.id);
            let tags = state
                .document_tags
                .get(&document.id)
                .map_or("—".into(), |tags| tags.join(", "));
            vec![
                Line::styled(document.title.clone(), theme.heading()),
                Line::from(""),
                Line::styled("Source", theme.section()),
                Line::styled(document.source.clone(), Style::default().fg(theme.text)),
                Line::from(""),
                Line::from(format!(
                    "Pages      {}",
                    document
                        .page_count
                        .map_or("?".into(), |value| value.to_string())
                )),
                Line::from(format!("Status     {}", document.status)),
                Line::from(format!("Parser     {}", document.parser_id)),
                Line::from(format!(
                    "Edition    {}",
                    document
                        .book
                        .as_ref()
                        .and_then(|book| book.edition_label.as_deref())
                        .unwrap_or("—")
                )),
                Line::from(format!(
                    "Evidence   {}% provenance",
                    document.quality.as_ref().map_or(0, |quality| {
                        (quality.provenance_coverage * 100.0).round() as u32
                    })
                )),
                Line::from(format!(
                    "Size       {}",
                    detail.map_or("unknown".into(), |item| format_bytes(item.size_bytes))
                )),
                Line::from(format!(
                    "Original   {}",
                    match document.archive_mode.as_str() {
                        "reflink" => "space-saving clone",
                        "copy" => "managed copy",
                        "existing" => "managed original reused",
                        "external" => "external source",
                        _ => "managed original",
                    }
                )),
                Line::from(format!("Tags       {tags}")),
            ]
        }
        View::Indexing | View::Activity => {
            let jobs = state.jobs.values().collect::<Vec<_>>();
            let Some(job) = jobs.get(state.job_cursor.min(jobs.len().saturating_sub(1))) else {
                return vec![Line::styled(
                    "No run selected.",
                    Style::default().fg(theme.muted),
                )];
            };
            let mut lines = vec![
                Line::styled(job.kind.clone(), theme.title()),
                Line::from(""),
                Line::from(format!("Status     {:?}", job.status)),
                Line::from(format!("Progress   {:.0}%", job.progress * 100.0)),
                Line::from(format!("Phase      {}", job.phase)),
            ];
            if let Some(detail) = &job.progress_detail {
                if let (Some(start), Some(end), Some(total)) =
                    (detail.page_start, detail.page_end, detail.total_pages)
                {
                    lines.push(Line::from(format!("Pages      {start}–{end} / {total}")));
                }
                lines.push(Line::from(format!("Memory     {}", detail.memory_state)));
                if let (Some(low), Some(high)) = (detail.eta_seconds_low, detail.eta_seconds_high) {
                    lines.push(Line::from(format!(
                        "ETA        {}–{}",
                        format_duration(low.round() as u64),
                        format_duration(high.round() as u64)
                    )));
                }
            }
            lines.push(Line::from(format!("Updated    {}", job.updated_at)));
            lines
        }
        View::Models => {
            let Some(entry) = state.model_manager.entries.get(state.model_manager.cursor) else {
                return vec![
                    Line::styled("Model", theme.section()),
                    Line::from(""),
                    Line::styled(
                        "Load the catalog and select a model.",
                        Style::default().fg(theme.muted),
                    ),
                ];
            };
            vec![
                Line::styled(entry.id.clone(), theme.heading()),
                Line::styled(entry.source.label(), Style::default().fg(theme.cyan)),
                Line::from(""),
                Line::styled(entry.description.clone(), Style::default().fg(theme.text)),
                Line::from(""),
                Line::from(format!("Category    {}", entry.category.label())),
                Line::from(format!("Fit         {}", entry.fit.label())),
                Line::from(format!(
                    "Memory      {}",
                    human_memory(entry.estimated_memory)
                )),
                Line::from(format!("Installed   {}", entry.installed)),
                Line::from(format!(
                    "Downloads   {}",
                    entry.downloads.map_or("—".into(), format_count)
                )),
                Line::from(format!(
                    "Likes       {}",
                    entry.likes.map_or("—".into(), format_count)
                )),
                Line::from(format!(
                    "Quant       {}",
                    entry.quantization.as_deref().unwrap_or("auto")
                )),
                Line::from(""),
                Line::styled(
                    entry.capabilities.join(" · "),
                    Style::default().fg(theme.muted),
                ),
            ]
        }
        View::FoundryOverview | View::System => {
            let mut lines = vec![
                Line::styled("Local runtime", theme.section()),
                Line::from(""),
                Line::from(format!(
                    "CPU       {:.0}% / {} threads",
                    metrics.cpu_usage, metrics.cpu_count
                )),
                Line::from(format!(
                    "Memory    {} / {}",
                    human_memory(metrics.memory_used),
                    human_memory(metrics.memory_total)
                )),
                Line::from(format!(
                    "GPU       {}",
                    metrics.gpu_name.as_deref().unwrap_or("system graphics")
                )),
                Line::from(format!(
                    "VRAM      {} / {}",
                    human_memory(metrics.vram_used),
                    human_memory(metrics.vram_total)
                )),
                Line::from(""),
                Line::styled("Resident models", theme.section()),
            ];
            if metrics.loaded_models.is_empty() {
                lines.push(Line::styled("○ none", Style::default().fg(theme.muted)));
            } else {
                lines.extend(metrics.loaded_models.iter().map(|model| {
                    Line::from(vec![
                        Span::styled("● ", Style::default().fg(theme.green)),
                        Span::styled(model.name.clone(), Style::default().fg(theme.text)),
                    ])
                }));
            }
            lines
        }
        View::History => vec![Line::styled(
            "Select a conversation to restore, rerun, edit, or export it.",
            Style::default().fg(theme.muted),
        )],
        View::Sources => state.sources.get(state.source_cursor).map_or_else(
            || {
                vec![Line::styled(
                    "Select a source.",
                    Style::default().fg(theme.muted),
                )]
            },
            |source| {
                vec![
                    Line::styled(source.name.clone(), theme.heading()),
                    Line::from(""),
                    Line::from(format!("Type      {}", source.source_type)),
                    Line::from(format!("Enabled   {}", source.enabled)),
                    Line::from(format!("Created   {}", source.created_at)),
                    Line::from(""),
                    Line::styled(source.location.clone(), Style::default().fg(theme.muted)),
                ]
            },
        ),
        View::Quality => vec![Line::styled(
            "Quality checks summarize ingestion health and surface actionable issues.",
            Style::default().fg(theme.muted),
        )],
        View::Backups => vec![Line::styled(
            "Backups are local, checksummed snapshots of this library.",
            Style::default().fg(theme.muted),
        )],
        View::Settings => vec![
            Line::styled(
                if state.config_dirty {
                    "Unsaved changes"
                } else {
                    "Saved"
                },
                Style::default()
                    .fg(if state.config_dirty {
                        theme.yellow
                    } else {
                        theme.green
                    })
                    .add_modifier(Modifier::BOLD),
            ),
            Line::from(""),
            Line::styled(
                "Settings apply to the active library. Ctrl+Enter writes the current YAML using its ETag.",
                Style::default().fg(theme.muted),
            ),
        ],
        View::Themes => {
            let palette = Theme::at(state.theme_cursor);
            let mut lines = vec![
                Line::styled(palette.name, theme.heading()),
                Line::from(""),
                Line::styled("Live preview", theme.section()),
                Line::from(vec![
                    Span::styled(
                        " Focus   ",
                        Style::default().fg(theme.background).bg(palette.focus),
                    ),
                    Span::raw("  "),
                    Span::styled(
                        " Ready   ",
                        Style::default().fg(theme.background).bg(palette.green),
                    ),
                ]),
                Line::from(vec![
                    Span::styled(
                        " Warning ",
                        Style::default().fg(theme.background).bg(palette.yellow),
                    ),
                    Span::raw("  "),
                    Span::styled(
                        " Error   ",
                        Style::default().fg(theme.background).bg(palette.red),
                    ),
                ]),
                Line::from(""),
                Line::styled(
                    "Enter keeps this palette. Esc restores the previous one.",
                    Style::default().fg(theme.muted),
                ),
            ];
            if state.theme_cursor == Theme::count() - 1 {
                lines.extend([
                    Line::from(""),
                    Line::styled(
                        if Theme::omarchy_available() {
                            "Following your desktop"
                        } else {
                            "Desktop palette unavailable · using a safe fallback"
                        },
                        Style::default().fg(if Theme::omarchy_available() {
                            theme.green
                        } else {
                            theme.yellow
                        }),
                    ),
                    Line::styled(
                        "Follows Omarchy color changes automatically.",
                        Style::default().fg(theme.muted),
                    ),
                ]);
            }
            lines
        }
    }
}

fn preferred_evidence_label<'a>(
    prompt_evidence_id: Option<&'a str>,
    stable_evidence_id: Option<&'a str>,
) -> &'a str {
    prompt_evidence_id.or(stable_evidence_id).unwrap_or("E?")
}

/// The run receipt, shown after the sources it describes and suppressed when
/// the run failed — reporting "source check passed" beside an error is a lie.
fn trailing_receipt_lines(state: &AppState, theme: &Theme) -> Vec<Line<'static>> {
    if state.chat.error.is_some() {
        return Vec::new();
    }
    let receipt = receipt_lines(state, theme);
    if receipt.is_empty() {
        return receipt;
    }
    let mut lines = vec![
        Line::from(""),
        Line::styled("Answer check", theme.section()),
    ];
    lines.extend(receipt);
    lines
}

fn receipt_lines(state: &AppState, theme: &Theme) -> Vec<Line<'static>> {
    let Some(receipt) = state.chat.receipt.as_ref() else {
        return Vec::new();
    };
    let (answer_label, answer_color) = match receipt.cache_status {
        AnswerCacheStatus::Hit => ("Saved answer", theme.green),
        AnswerCacheStatus::Miss => ("Fresh answer", theme.cyan),
        AnswerCacheStatus::Bypass => ("Fresh answer", theme.cyan),
    };
    let (check_label, check_color) = match receipt.source_check {
        SourceCheck::Verified => ("Passed", theme.green),
        SourceCheck::Reviewed => ("Reviewed", theme.yellow),
        SourceCheck::Insufficient => ("Not enough evidence", theme.red),
    };
    let mut lines = vec![
        Line::from(vec![
            Span::styled(answer_label, Style::default().fg(answer_color)),
            Span::styled(
                format!(" · {}", format_milliseconds(receipt.total_ms)),
                Style::default().fg(theme.muted),
            ),
        ]),
        Line::from(format!("Conversation · question {}", receipt.turn)),
        Line::from(format!("Sources checked · {}", receipt.source_count)),
    ];
    if receipt.turn > 1 {
        lines.push(Line::from(format!(
            "Known sources · {} · New · {}",
            receipt.reused_source_count, receipt.new_source_count
        )));
    }
    lines.push(Line::from(vec![
        Span::raw("Source check · "),
        Span::styled(check_label, Style::default().fg(check_color)),
    ]));
    lines.push(Line::from(format!(
        "Retrieval · {} · Reranker · {}",
        receipt.retrieval_mode, receipt.rerank_status
    )));
    if receipt.cache_status == AnswerCacheStatus::Hit {
        lines.push(Line::styled(
            "Same books and settings",
            Style::default().fg(theme.muted),
        ));
    }
    lines
}

fn format_milliseconds(milliseconds: f64) -> String {
    if milliseconds < 1_000.0 {
        format!("{milliseconds:.0} ms")
    } else {
        format!("{:.1} s", milliseconds / 1_000.0)
    }
}

pub(crate) fn file_browser_areas(screen: Rect) -> [Rect; 4] {
    let height = screen.height.saturating_sub(4).clamp(12, 36);
    let area = centered(88, height, screen);
    let inner = Rect::new(
        area.x.saturating_add(1),
        area.y.saturating_add(1),
        area.width.saturating_sub(2),
        area.height.saturating_sub(2),
    );
    let [body, footer] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(2)]).areas(inner);
    let [list, selected] =
        Layout::horizontal([Constraint::Percentage(68), Constraint::Percentage(32)]).areas(body);
    [area, list, selected, footer]
}

pub(crate) fn confirm_import_area(screen: Rect) -> Rect {
    centered(72, 22, screen)
}

pub(crate) fn foundry_setup_areas(area: Rect) -> [Rect; 4] {
    Layout::vertical([
        Constraint::Length(3),
        Constraint::Length(4),
        Constraint::Fill(1),
        Constraint::Length(2),
    ])
    .areas(area)
}

pub(crate) fn foundry_catalog_areas(area: Rect) -> [Rect; 4] {
    Layout::vertical([
        Constraint::Length(2),
        Constraint::Length(2),
        Constraint::Fill(1),
        Constraint::Length(2),
    ])
    .areas(area)
}

pub(crate) fn catalog_filter_areas(area: Rect) -> [Rect; 3] {
    Layout::horizontal([
        Constraint::Percentage(35),
        Constraint::Percentage(35),
        Constraint::Percentage(30),
    ])
    .areas(area)
}

pub(crate) fn delete_model_confirm_area(screen: Rect) -> Rect {
    centered(52, 9, screen)
}

pub(crate) fn confirm_quit_area(screen: Rect) -> Rect {
    centered(54, 5, screen)
}

fn highlighted_answer(answer: &str, selected: usize, theme: &Theme) -> Text<'static> {
    let mut options = MarkdownOptions::empty();
    options.insert(MarkdownOptions::ENABLE_TABLES);
    options.insert(MarkdownOptions::ENABLE_MATH);
    options.insert(MarkdownOptions::ENABLE_STRIKETHROUGH);
    options.insert(MarkdownOptions::ENABLE_TASKLISTS);
    options.insert(MarkdownOptions::ENABLE_FOOTNOTES);

    let mut lines = Vec::<Line<'static>>::new();
    let mut spans = Vec::<Span<'static>>::new();
    let mut styles = vec![Style::default().fg(theme.text)];
    let mut lists = Vec::<(Option<u64>, u64)>::new();
    let mut quote_depth = 0usize;
    let mut link_targets = Vec::<String>::new();
    let mut in_table_row = false;
    let mut table_cell = 0usize;
    let answer = prepare_answer_markdown(answer);

    for event in Parser::new_ext(&answer, options) {
        match event {
            MarkdownEvent::Start(tag) => match tag {
                Tag::Paragraph => finish_markdown_line(&mut lines, &mut spans, false),
                Tag::Heading { level, .. } => {
                    finish_markdown_line(&mut lines, &mut spans, false);
                    let prefix = match level {
                        HeadingLevel::H1 => "◆ ",
                        HeadingLevel::H2 => "◇ ",
                        _ => "› ",
                    };
                    spans.push(Span::styled(prefix, theme.heading()));
                    styles.push(
                        current_markdown_style(&styles)
                            .fg(theme.focus)
                            .add_modifier(Modifier::BOLD),
                    );
                }
                Tag::Strong => {
                    styles.push(current_markdown_style(&styles).add_modifier(Modifier::BOLD))
                }
                Tag::Emphasis => {
                    styles.push(current_markdown_style(&styles).add_modifier(Modifier::ITALIC))
                }
                Tag::Strikethrough => {
                    styles.push(current_markdown_style(&styles).add_modifier(Modifier::CROSSED_OUT))
                }
                Tag::BlockQuote(_) => {
                    finish_markdown_line(&mut lines, &mut spans, false);
                    quote_depth += 1;
                    spans.push(Span::styled(
                        format!("{}│ ", "  ".repeat(quote_depth.saturating_sub(1))),
                        Style::default().fg(theme.purple),
                    ));
                    styles.push(current_markdown_style(&styles).fg(theme.muted));
                }
                Tag::CodeBlock(_) => {
                    finish_markdown_line(&mut lines, &mut spans, false);
                    styles.push(
                        current_markdown_style(&styles)
                            .fg(theme.yellow)
                            .bg(theme.panel),
                    );
                }
                Tag::List(start) => {
                    finish_markdown_line(&mut lines, &mut spans, false);
                    lists.push((start, start.unwrap_or(1)));
                }
                Tag::Item => {
                    finish_markdown_line(&mut lines, &mut spans, false);
                    let depth = lists.len().saturating_sub(1);
                    let prefix = lists.last_mut().map_or("• ".into(), |(start, next)| {
                        if start.is_some() {
                            let value = format!("{next}. ");
                            *next += 1;
                            value
                        } else {
                            "• ".into()
                        }
                    });
                    spans.push(Span::styled(
                        format!("{}{prefix}", "  ".repeat(depth)),
                        Style::default().fg(theme.cyan),
                    ));
                }
                Tag::Link { dest_url, .. } => {
                    styles.push(
                        current_markdown_style(&styles)
                            .fg(theme.cyan)
                            .add_modifier(Modifier::UNDERLINED),
                    );
                    link_targets.push(dest_url.into_string());
                }
                Tag::Image { dest_url, .. } => {
                    spans.push(Span::styled("▧ ", Style::default().fg(theme.purple)));
                    styles.push(current_markdown_style(&styles).fg(theme.purple));
                    link_targets.push(dest_url.into_string());
                }
                Tag::Table(_) => finish_markdown_line(&mut lines, &mut spans, false),
                Tag::TableHead => {
                    finish_markdown_line(&mut lines, &mut spans, false);
                    in_table_row = true;
                    table_cell = 0;
                    styles.push(
                        current_markdown_style(&styles)
                            .fg(theme.focus)
                            .add_modifier(Modifier::BOLD),
                    );
                }
                Tag::TableRow => {
                    finish_markdown_line(&mut lines, &mut spans, false);
                    in_table_row = true;
                    table_cell = 0;
                }
                Tag::TableCell => {
                    if table_cell > 0 {
                        spans.push(Span::styled(" │ ", Style::default().fg(theme.border)));
                    }
                    table_cell += 1;
                }
                _ => {}
            },
            MarkdownEvent::End(tag) => match tag {
                TagEnd::Paragraph => finish_markdown_line(&mut lines, &mut spans, true),
                TagEnd::Heading(_) => {
                    styles.pop();
                    finish_markdown_line(&mut lines, &mut spans, true);
                }
                TagEnd::Strong | TagEnd::Emphasis | TagEnd::Strikethrough => {
                    styles.pop();
                }
                TagEnd::BlockQuote(_) => {
                    styles.pop();
                    quote_depth = quote_depth.saturating_sub(1);
                    finish_markdown_line(&mut lines, &mut spans, true);
                }
                TagEnd::CodeBlock => {
                    styles.pop();
                    finish_markdown_line(&mut lines, &mut spans, true);
                }
                TagEnd::List(_) => {
                    lists.pop();
                    finish_markdown_line(&mut lines, &mut spans, false);
                }
                TagEnd::Item => finish_markdown_line(&mut lines, &mut spans, false),
                TagEnd::Link | TagEnd::Image => {
                    styles.pop();
                    if let Some(target) = link_targets.pop()
                        && !target.is_empty()
                    {
                        spans.push(Span::styled(
                            format!(" ↗{}", sanitize_terminal_text(&target)),
                            Style::default().fg(theme.muted),
                        ));
                    }
                }
                TagEnd::TableHead => {
                    styles.pop();
                    in_table_row = false;
                    finish_markdown_line(&mut lines, &mut spans, false);
                }
                TagEnd::TableRow => {
                    in_table_row = false;
                    finish_markdown_line(&mut lines, &mut spans, false);
                }
                TagEnd::Table => finish_markdown_line(&mut lines, &mut spans, true),
                _ => {}
            },
            MarkdownEvent::Text(text) => push_markdown_text(
                &mut spans,
                &text,
                current_markdown_style(&styles),
                selected,
                theme,
            ),
            MarkdownEvent::Code(code) => push_markdown_text(
                &mut spans,
                &code,
                current_markdown_style(&styles)
                    .fg(theme.yellow)
                    .bg(theme.panel),
                selected,
                theme,
            ),
            MarkdownEvent::SoftBreak | MarkdownEvent::HardBreak => {
                finish_markdown_line(&mut lines, &mut spans, false);
                if quote_depth > 0 {
                    spans.push(Span::styled(
                        format!("{}│ ", "  ".repeat(quote_depth.saturating_sub(1))),
                        Style::default().fg(theme.purple),
                    ));
                }
            }
            MarkdownEvent::Rule => {
                finish_markdown_line(&mut lines, &mut spans, false);
                lines.push(Line::styled(
                    "─".repeat(28),
                    Style::default().fg(theme.border),
                ));
            }
            MarkdownEvent::TaskListMarker(done) => spans.push(Span::styled(
                if done { "[✓] " } else { "[ ] " },
                Style::default().fg(if done { theme.green } else { theme.muted }),
            )),
            MarkdownEvent::FootnoteReference(reference) => spans.push(Span::styled(
                format!("[{}]", sanitize_terminal_text(&reference)),
                Style::default().fg(theme.purple),
            )),
            MarkdownEvent::InlineMath(math) => spans.push(Span::styled(
                math_to_unicode(&math),
                Style::default().fg(theme.yellow),
            )),
            MarkdownEvent::DisplayMath(math) => {
                finish_markdown_line(&mut lines, &mut spans, false);
                lines.push(Line::styled(
                    math_to_unicode(&math),
                    Style::default().fg(theme.yellow),
                ));
            }
            MarkdownEvent::Html(_) | MarkdownEvent::InlineHtml(_) => {}
        }
        if in_table_row && spans.is_empty() {
            in_table_row = false;
        }
    }
    finish_markdown_line(&mut lines, &mut spans, false);
    while lines.last().is_some_and(|line| line.spans.is_empty()) {
        lines.pop();
    }
    Text::from(lines)
}

fn prepare_answer_markdown(answer: &str) -> String {
    static FIGURE_REFERENCE: OnceLock<Regex> = OnceLock::new();
    static FIGURE_CAPTION: OnceLock<Regex> = OnceLock::new();
    let reference = FIGURE_REFERENCE.get_or_init(|| {
        Regex::new(
            r"(?i)(?:\(\s*(?:siehe\s+)?(?:abb\.?|abbildung)\s*\d+[a-z]?(?:\s*[-–]\s*\d+[a-z]?)?\s*\)|\b(?:siehe\s+)?(?:abb\.?|abbildung)\s*\d+[a-z]?\b)",
        )
        .expect("valid figure-reference regex")
    });
    let caption = FIGURE_CAPTION.get_or_init(|| {
        Regex::new(r"(?i)^\s*(?:abb\.?|abbildung)\s*\d+[a-z]?\s*[:.]?")
            .expect("valid figure-caption regex")
    });

    answer
        .replace("\\(", "$")
        .replace("\\)", "$")
        .replace("\\[", "$$\n")
        .replace("\\]", "\n$$")
        .lines()
        .filter_map(|line| {
            if caption.is_match(line) {
                return None;
            }
            let clean = reference.replace_all(line, "");
            Some(clean.trim_end().to_owned())
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn math_to_unicode(math: &str) -> String {
    let mut rendered = sanitize_terminal_text(math).trim().to_owned();
    for (latex, unicode) in [
        ("\\varepsilon", "ε"),
        ("\\vartheta", "ϑ"),
        ("\\varphi", "φ"),
        ("\\varrho", "ϱ"),
        ("\\upsilon", "υ"),
        ("\\epsilon", "ε"),
        ("\\lambda", "λ"),
        ("\\omicron", "ο"),
        ("\\theta", "θ"),
        ("\\kappa", "κ"),
        ("\\sigma", "σ"),
        ("\\omega", "ω"),
        ("\\alpha", "α"),
        ("\\gamma", "γ"),
        ("\\delta", "δ"),
        ("\\beta", "β"),
        ("\\zeta", "ζ"),
        ("\\eta", "η"),
        ("\\iota", "ι"),
        ("\\mu", "μ"),
        ("\\nu", "ν"),
        ("\\xi", "ξ"),
        ("\\pi", "π"),
        ("\\rho", "ρ"),
        ("\\tau", "τ"),
        ("\\phi", "ϕ"),
        ("\\chi", "χ"),
        ("\\psi", "ψ"),
        ("\\cdot", "·"),
        ("\\times", "×"),
        ("\\approx", "≈"),
        ("\\neq", "≠"),
        ("\\leq", "≤"),
        ("\\le", "≤"),
        ("\\geq", "≥"),
        ("\\ge", "≥"),
        ("\\pm", "±"),
        ("\\rightarrow", "→"),
        ("\\leftarrow", "←"),
        ("\\infty", "∞"),
        ("\\degree", "°"),
    ] {
        rendered = rendered.replace(latex, unicode);
    }
    rendered = rendered
        .replace("\\mathrm{", "")
        .replace("\\text{", "")
        .replace("\\,", " ")
        .replace("\\;", " ");
    rendered = replace_math_script(&rendered, '_', subscript_character);
    rendered = replace_math_script(&rendered, '^', superscript_character);
    rendered.replace(['{', '}'], "")
}

fn replace_math_script(value: &str, marker: char, convert: fn(char) -> Option<char>) -> String {
    let characters = value.chars().collect::<Vec<_>>();
    let mut output = String::with_capacity(value.len());
    let mut index = 0usize;
    while index < characters.len() {
        if characters[index] != marker {
            output.push(characters[index]);
            index += 1;
            continue;
        }
        let mut cursor = index + 1;
        let grouped = characters.get(cursor) == Some(&'{');
        if grouped {
            cursor += 1;
        }
        let start = cursor;
        while cursor < characters.len()
            && if grouped {
                characters[cursor] != '}'
            } else {
                cursor == start
            }
        {
            cursor += 1;
        }
        let converted = characters[start..cursor]
            .iter()
            .copied()
            .map(convert)
            .collect::<Option<String>>();
        if let Some(converted) = converted {
            output.push_str(&converted);
            index = cursor + usize::from(grouped && characters.get(cursor) == Some(&'}'));
        } else {
            output.push(marker);
            index += 1;
        }
    }
    output
}

fn subscript_character(character: char) -> Option<char> {
    Some(match character {
        '0' => '₀',
        '1' => '₁',
        '2' => '₂',
        '3' => '₃',
        '4' => '₄',
        '5' => '₅',
        '6' => '₆',
        '7' => '₇',
        '8' => '₈',
        '9' => '₉',
        '+' => '₊',
        '-' => '₋',
        '=' => '₌',
        '(' => '₍',
        ')' => '₎',
        'a' => 'ₐ',
        'e' => 'ₑ',
        'h' => 'ₕ',
        'i' => 'ᵢ',
        'j' => 'ⱼ',
        'k' => 'ₖ',
        'l' => 'ₗ',
        'm' => 'ₘ',
        'n' => 'ₙ',
        'o' => 'ₒ',
        'p' => 'ₚ',
        'r' => 'ᵣ',
        's' => 'ₛ',
        't' => 'ₜ',
        'u' => 'ᵤ',
        'v' => 'ᵥ',
        'x' => 'ₓ',
        _ => return None,
    })
}

fn superscript_character(character: char) -> Option<char> {
    Some(match character {
        '0' => '⁰',
        '1' => '¹',
        '2' => '²',
        '3' => '³',
        '4' => '⁴',
        '5' => '⁵',
        '6' => '⁶',
        '7' => '⁷',
        '8' => '⁸',
        '9' => '⁹',
        '+' => '⁺',
        '-' => '⁻',
        '=' => '⁼',
        '(' => '⁽',
        ')' => '⁾',
        'i' => 'ⁱ',
        'n' => 'ⁿ',
        _ => return None,
    })
}

#[derive(Debug, Clone)]
struct AnswerGlyph {
    character: char,
    style: Style,
    offset: usize,
}

#[derive(Debug, Clone)]
struct AnswerVisualLayout {
    rows: Vec<Vec<AnswerGlyph>>,
    plain: Vec<char>,
    bold: Vec<bool>,
}

fn answer_visual_layout(
    answer: &str,
    selected_citation: usize,
    theme: &Theme,
    width: u16,
) -> AnswerVisualLayout {
    let text = highlighted_answer(answer, selected_citation, theme);
    let mut logical_lines = Vec::<Vec<AnswerGlyph>>::with_capacity(text.lines.len());
    let mut plain = Vec::<char>::new();
    let mut bold = Vec::<bool>::new();
    for (line_index, line) in text.lines.iter().enumerate() {
        let mut glyphs = Vec::new();
        for span in &line.spans {
            for character in span.content.chars() {
                let offset = plain.len();
                plain.push(character);
                bold.push(span.style.add_modifier.contains(Modifier::BOLD));
                glyphs.push(AnswerGlyph {
                    character,
                    style: span.style,
                    offset,
                });
            }
        }
        logical_lines.push(glyphs);
        if line_index + 1 < text.lines.len() {
            plain.push('\n');
            bold.push(false);
        }
    }

    let mut rows = Vec::new();
    let width = usize::from(width.max(1));
    for glyphs in logical_lines {
        if glyphs.is_empty() {
            rows.push(Vec::new());
            continue;
        }
        let mut start = 0;
        while start < glyphs.len() {
            let mut used = 0usize;
            let mut end = start;
            while end < glyphs.len() {
                let character_width = glyphs[end].character.width().unwrap_or(0).max(1);
                if end > start && used.saturating_add(character_width) > width {
                    break;
                }
                used = used.saturating_add(character_width);
                end += 1;
                if used >= width {
                    break;
                }
            }
            if end == glyphs.len() {
                rows.push(glyphs[start..end].to_vec());
                break;
            }
            let word_break = (start + 1..end)
                .rev()
                .find(|index| glyphs[*index].character.is_whitespace());
            if let Some(word_break) = word_break {
                rows.push(glyphs[start..word_break].to_vec());
                start = word_break + 1;
                while start < glyphs.len() && glyphs[start].character == ' ' {
                    start += 1;
                }
            } else {
                rows.push(glyphs[start..end].to_vec());
                start = end;
            }
        }
    }
    AnswerVisualLayout { rows, plain, bold }
}

fn selectable_answer(
    answer: &str,
    selected_citation: usize,
    theme: &Theme,
    width: u16,
    selection: Option<ChatTextSelection>,
) -> Text<'static> {
    let layout = answer_visual_layout(answer, selected_citation, theme, width);
    let selected = selection.map(ChatTextSelection::bounds);
    let lines = layout
        .rows
        .into_iter()
        .map(|row| {
            let mut spans = Vec::<Span<'static>>::new();
            let mut text = String::new();
            let mut active_style = None;
            for glyph in row {
                let mut style = glyph.style;
                if selected.is_some_and(|(start, end)| glyph.offset >= start && glyph.offset <= end)
                {
                    style = style.bg(theme.selection);
                }
                if active_style.is_some_and(|current| current != style) && !text.is_empty() {
                    spans.push(Span::styled(
                        std::mem::take(&mut text),
                        active_style.unwrap(),
                    ));
                }
                active_style = Some(style);
                text.push(glyph.character);
            }
            if !text.is_empty() {
                spans.push(Span::styled(text, active_style.unwrap_or_default()));
            }
            Line::from(spans)
        })
        .collect::<Vec<_>>();
    Text::from(lines)
}

pub(crate) fn chat_answer_offset(
    answer: &str,
    selected_citation: usize,
    width: u16,
    scroll: u16,
    column: u16,
    row: u16,
) -> Option<usize> {
    let layout = answer_visual_layout(answer, selected_citation, &Theme::default(), width);
    let glyphs = layout.rows.get(usize::from(scroll.saturating_add(row)))?;
    let mut x = 0u16;
    for glyph in glyphs {
        let glyph_width = u16::try_from(glyph.character.width().unwrap_or(0).max(1)).ok()?;
        if column >= x && column < x.saturating_add(glyph_width) {
            return Some(glyph.offset);
        }
        x = x.saturating_add(glyph_width);
    }
    None
}

pub(crate) fn chat_selection_text(
    answer: &str,
    selected_citation: usize,
    width: u16,
    selection: ChatTextSelection,
) -> Option<String> {
    let layout = answer_visual_layout(answer, selected_citation, &Theme::default(), width);
    let (start, end) = selection.bounds();
    if start >= layout.plain.len() {
        return None;
    }
    let selected = layout.plain[start..=end.min(layout.plain.len().saturating_sub(1))]
        .iter()
        .collect::<String>();
    let selected = selected.trim().to_owned();
    (!selected.is_empty()).then_some(selected)
}

pub(crate) fn chat_bold_term_at(
    answer: &str,
    selected_citation: usize,
    width: u16,
    scroll: u16,
    column: u16,
    row: u16,
) -> Option<String> {
    let offset = chat_answer_offset(answer, selected_citation, width, scroll, column, row)?;
    let layout = answer_visual_layout(answer, selected_citation, &Theme::default(), width);
    if !layout.bold.get(offset).copied().unwrap_or(false) {
        return None;
    }
    for term in markdown_strong_terms(answer) {
        let needle = term.chars().collect::<Vec<_>>();
        if needle.is_empty() || needle.len() > layout.plain.len() {
            continue;
        }
        for start in 0..=layout.plain.len() - needle.len() {
            if layout.plain[start..start + needle.len()] == needle
                && offset >= start
                && offset < start + needle.len()
            {
                return Some(term);
            }
        }
    }
    None
}

fn markdown_strong_terms(answer: &str) -> Vec<String> {
    let mut terms = Vec::new();
    let mut depth = 0usize;
    let mut term = String::new();
    for event in Parser::new_ext(answer, MarkdownOptions::all()) {
        match event {
            MarkdownEvent::Start(Tag::Strong) => {
                if depth == 0 {
                    term.clear();
                }
                depth += 1;
            }
            MarkdownEvent::End(TagEnd::Strong) => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    let clean = sanitize_terminal_text(term.trim());
                    if !clean.is_empty() {
                        terms.push(clean);
                    }
                }
            }
            MarkdownEvent::Text(text) | MarkdownEvent::Code(text) if depth > 0 => {
                term.push_str(&text);
            }
            MarkdownEvent::SoftBreak | MarkdownEvent::HardBreak if depth > 0 => term.push(' '),
            _ => {}
        }
    }
    terms
}

fn current_markdown_style(styles: &[Style]) -> Style {
    styles.last().copied().unwrap_or_default()
}

fn finish_markdown_line(
    lines: &mut Vec<Line<'static>>,
    spans: &mut Vec<Span<'static>>,
    blank_after: bool,
) {
    if !spans.is_empty() {
        lines.push(Line::from(std::mem::take(spans)));
    }
    if blank_after && lines.last().is_some_and(|line| !line.spans.is_empty()) {
        lines.push(Line::from(""));
    }
}

fn push_markdown_text(
    spans: &mut Vec<Span<'static>>,
    text: &str,
    style: Style,
    selected: usize,
    theme: &Theme,
) {
    let sanitized = sanitize_terminal_text(text);
    let mut rest = sanitized.as_str();
    while let Some(start) = rest.find('[') {
        if start > 0 {
            spans.push(Span::styled(rest[..start].to_owned(), style));
        }
        let Some(relative_end) = rest[start..].find(']') else {
            spans.push(Span::styled(rest[start..].to_owned(), style));
            return;
        };
        let end = start + relative_end;
        let marker = &rest[start..=end];
        let token = marker.trim_matches(['[', ']']);
        let index = token
            .strip_prefix(['E', 'e'])
            .unwrap_or(token)
            .parse::<usize>()
            .ok();
        if let Some(index) = index {
            spans.push(Span::styled(
                marker.to_owned(),
                Style::default()
                    .fg(if index == selected + 1 {
                        theme.background
                    } else {
                        theme.orange
                    })
                    .bg(if index == selected + 1 {
                        theme.focus
                    } else {
                        theme.panel
                    })
                    .add_modifier(Modifier::BOLD),
            ));
        } else {
            spans.push(Span::styled(marker.to_owned(), style));
        }
        rest = &rest[end + 1..];
    }
    if !rest.is_empty() {
        spans.push(Span::styled(rest.to_owned(), style));
    }
}

fn sanitize_terminal_text(value: &str) -> String {
    #[derive(Clone, Copy)]
    enum EscapeState {
        Text,
        Escape,
        ControlSequence,
        OperatingSystemCommand,
        OperatingSystemCommandEscape,
    }

    let mut state = EscapeState::Text;
    let mut output = String::with_capacity(value.len());
    for character in value.chars() {
        state = match state {
            EscapeState::Text if character == '\u{1b}' => EscapeState::Escape,
            EscapeState::Text => {
                if character == '\t' {
                    output.push(' ');
                } else if !character.is_control() {
                    output.push(character);
                }
                EscapeState::Text
            }
            EscapeState::Escape => match character {
                '[' => EscapeState::ControlSequence,
                ']' => EscapeState::OperatingSystemCommand,
                _ => EscapeState::Text,
            },
            EscapeState::ControlSequence if ('@'..='~').contains(&character) => EscapeState::Text,
            EscapeState::ControlSequence => EscapeState::ControlSequence,
            EscapeState::OperatingSystemCommand if character == '\u{7}' => EscapeState::Text,
            EscapeState::OperatingSystemCommand if character == '\u{1b}' => {
                EscapeState::OperatingSystemCommandEscape
            }
            EscapeState::OperatingSystemCommand => EscapeState::OperatingSystemCommand,
            EscapeState::OperatingSystemCommandEscape if character == '\\' => EscapeState::Text,
            EscapeState::OperatingSystemCommandEscape => EscapeState::OperatingSystemCommand,
        };
    }
    output
}

/// A row of actions with the triggering letter highlighted in the one accent.
fn shortcut_words(theme: &Theme, words: &[(&str, char)]) -> Line<'static> {
    let mut spans = Vec::new();
    for (index, (word, key)) in words.iter().enumerate() {
        if index > 0 {
            spans.push(Span::raw("   "));
        }
        spans.extend(shortcut_word(theme, word, *key));
    }
    Line::from(spans)
}

fn shortcut_word(theme: &Theme, word: &str, key: char) -> Vec<Span<'static>> {
    let lower = word.to_ascii_lowercase();
    let needle = key.to_ascii_lowercase().to_string();
    let position = lower.find(&needle).unwrap_or_default();
    let end = position + key.len_utf8();
    vec![
        Span::styled(word[..position].to_owned(), theme.body()),
        Span::styled(word[position..end].to_owned(), theme.key()),
        Span::styled(word[end..].to_owned(), theme.body()),
    ]
}

fn library_jobs(state: &AppState) -> Vec<&JobSnapshot> {
    state
        .jobs
        .values()
        .filter(|job| {
            if job.kind != "ingest"
                || state.hidden_jobs.contains(&job.id)
                || matches!(job.status, JobStatus::Completed | JobStatus::Cancelled)
            {
                return false;
            }
            match state.library.filter {
                LibraryFilter::All => true,
                LibraryFilter::Indexing => job.status != JobStatus::Failed,
                LibraryFilter::Failed => job.status == JobStatus::Failed,
                LibraryFilter::Ready | LibraryFilter::Duplicates => false,
            }
        })
        .collect()
}

fn library_documents(state: &AppState) -> Vec<&omarag_domain::DocumentSummary> {
    let query = state.library.query.value.trim();
    let mut documents = state
        .documents
        .iter()
        .filter(|document| {
            let search_text = format!(
                "{} {} {} {} {} {}",
                document.title,
                document.source,
                document.status,
                document
                    .book
                    .as_ref()
                    .map_or(String::new(), |book| book.authors.join(" ")),
                document
                    .book
                    .as_ref()
                    .and_then(|book| book.edition_label.clone())
                    .unwrap_or_default(),
                state
                    .document_tags
                    .get(&document.id)
                    .map_or(String::new(), |tags| tags.join(" "))
            );
            let matches_query = fuzzy_score(&search_text, query).is_some();
            let hash = state
                .library
                .details
                .get(&document.id)
                .and_then(|detail| detail.sha256.as_ref());
            let duplicate = state
                .documents
                .iter()
                .filter(|other| {
                    other.source == document.source
                        || hash.is_some_and(|hash| {
                            state
                                .library
                                .details
                                .get(&other.id)
                                .and_then(|detail| detail.sha256.as_ref())
                                == Some(hash)
                        })
                })
                .count()
                > 1;
            matches_query
                && match state.library.filter {
                    LibraryFilter::All | LibraryFilter::Ready => true,
                    LibraryFilter::Duplicates => duplicate,
                    LibraryFilter::Indexing | LibraryFilter::Failed => false,
                }
        })
        .collect::<Vec<_>>();
    documents.sort_by(|left, right| {
        if !query.is_empty() {
            let search_text = |document: &omarag_domain::DocumentSummary| {
                format!(
                    "{} {} {} {}",
                    document.title,
                    document.source,
                    document.status,
                    state
                        .document_tags
                        .get(&document.id)
                        .map_or(String::new(), |tags| tags.join(" "))
                )
            };
            let left_score = fuzzy_score(&search_text(left), query).unwrap_or_default();
            let right_score = fuzzy_score(&search_text(right), query).unwrap_or_default();
            return right_score.cmp(&left_score);
        }
        match state.library.sort {
            LibrarySort::Newest => right.imported_at.cmp(&left.imported_at),
            LibrarySort::Title => left.title.to_lowercase().cmp(&right.title.to_lowercase()),
            LibrarySort::Size => state
                .library
                .details
                .get(&right.id)
                .map_or(0, |item| item.size_bytes)
                .cmp(
                    &state
                        .library
                        .details
                        .get(&left.id)
                        .map_or(0, |item| item.size_bytes),
                ),
        }
    });
    documents
}

fn render_footer(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let mode = if state.input_mode == InputMode::Text {
        "TYPE"
    } else {
        "NAV"
    };
    let hints: &[(&str, &str)] = match state.overlay {
        Some(Overlay::ConfirmQuit) => &[("Enter / Y", "Quit"), ("Esc / N", "Stay")],
        Some(Overlay::AutomaticStackPreflight)
            if state
                .automatic_stack_preflight
                .as_ref()
                .is_some_and(|preflight| preflight.requires_reindex) =>
        {
            &[("Enter", "Close"), ("Esc", "Close")]
        }
        Some(Overlay::AutomaticStackPreflight) => {
            &[("Enter / Y", "Continue"), ("Esc / N", "Cancel")]
        }
        Some(Overlay::AutomaticStackDownloadConfirm) => {
            &[("D", "Download & apply"), ("Esc / N", "Cancel")]
        }
        Some(Overlay::FileBrowser) => &[
            ("↑↓", "Select"),
            ("Space", "Mark"),
            ("Enter", "Review"),
            ("F", "Favorite"),
            ("R", "Recent"),
            ("Esc", "Close"),
        ],
        Some(Overlay::ConfirmImport) => &[("Enter", "Queue"), ("Esc", "Back")],
        Some(Overlay::ChatHistory) => &[
            ("↑↓", "Select"),
            ("Enter", "Restore"),
            ("R", "Rerun"),
            ("E", "Edit"),
            ("X", "Export"),
        ],
        Some(Overlay::BookScope) => &[("↑↓", "choose"), ("Enter", "apply"), ("Esc", "cancel")],
        Some(Overlay::Help) => &[("Esc", "close")],
        Some(Overlay::Palette) => &[("↑↓", "choose"), ("Enter", "run"), ("Esc", "close")],
        Some(Overlay::DocumentDetails) => &[("Enter", "open PDF"), ("Esc", "close")],
        Some(Overlay::WorkspaceProfile) => &[("↑↓", "Select"), ("Enter", "Apply"), ("Esc", "Back")],
        Some(Overlay::CustomProfileEditor) => &[
            ("Tab", "Field"),
            ("←→", "Change"),
            ("Enter", "Save"),
            ("Esc", "Back"),
        ],
        Some(Overlay::ConfirmLibraryDelete) => &[
            ("Enter", "Unregister"),
            ("Shift+D", "Delete"),
            ("Esc", "Back"),
        ],
        Some(_) => &[("Enter", "Apply"), ("Esc", "Back"), ("Mouse", "Choose")],
        None if state.input_mode == InputMode::Text => {
            &[("Enter", "Apply"), ("Esc", "Finish"), ("←→", "Cursor")]
        }
        None => match state.focus_pane {
            FocusPane::Sidebar => &[
                ("↑↓", "Navigate"),
                ("Enter", "Open"),
                ("I", "Add PDFs"),
                ("?", "Help"),
            ],
            FocusPane::Workspace => match state.view {
                View::Conversation => &[
                    ("Enter", "Ask"),
                    ("Drag", "Copy"),
                    ("[ ]", "Sources"),
                    ("H", "History"),
                ],
                View::Books => &[
                    ("↑↓", "Select"),
                    ("Enter", "Open"),
                    ("I", "Add PDFs"),
                    ("/", "Search"),
                ],
                View::FoundryOverview | View::Models => &[
                    ("↑↓", "Select"),
                    ("[ ]", "Role"),
                    ("/", "Search"),
                    ("R", "Refresh"),
                ],
                View::Indexing | View::Activity => {
                    &[("↑↓", "Select"), ("Space", "Pause/resume"), ("X", "Cancel")]
                }
                View::Themes => &[
                    ("↑↓", "Choose"),
                    ("Enter", "Apply"),
                    ("Esc", "Cancel"),
                    ("Ctrl+T", "Next"),
                ],
                View::Settings => &[
                    ("B", "explain terms"),
                    ("K", "icons"),
                    ("G", "glyphs"),
                    ("M", "simple/advanced"),
                ],
                _ => &[("↑↓", "Move"), ("Enter", "Open"), ("Tab", "Next pane")],
            },
            FocusPane::Inspector => &[
                ("↑↓", "Source"),
                ("←→", "Page"),
                ("Enter", "Open PDF"),
                ("Space", "Image"),
            ],
        },
    };
    let mut spans = vec![Span::styled(
        format!(" {mode} "),
        Style::default()
            .fg(theme.background)
            .bg(if state.input_mode == InputMode::Text {
                theme.yellow
            } else {
                theme.focus
            })
            .add_modifier(Modifier::BOLD),
    )];
    for (key, label) in hints {
        spans.push(Span::styled(format!("  {key}"), theme.key()));
        spans.push(Span::styled(format!(" {label}"), theme.meta()));
    }
    if state.undo.is_some() {
        spans.push(Span::styled("  Ctrl+Z", theme.key()));
        spans.push(Span::styled(" undo", theme.meta()));
    }
    if state.overlay.is_none() {
        // Below the wide breakpoint some regions are off screen, so say how to
        // reach them rather than letting them look absent.
        if area.width < 120 {
            spans.push(Span::styled("  Tab", theme.key()));
            spans.push(Span::styled(
                if area.width < 96 {
                    " nav · sources"
                } else {
                    " sources"
                },
                theme.meta(),
            ));
        }
        // Some views already list `?` themselves; do not say it twice.
        if !hints.iter().any(|(key, _)| key.contains('?')) {
            spans.push(Span::styled("  ?", theme.key()));
            spans.push(Span::styled(" help", theme.meta()));
        }
    }
    frame.render_widget(Paragraph::new(Line::from(spans)).style(theme.meta()), area);
}

/// Dims everything behind a modal so the dialog reads as the only live surface.
/// Without it the workspace text keeps full contrast right up to the dialog edge
/// and the two layers visually interleave.
fn render_overlay_scrim(frame: &mut Frame<'_>, theme: &Theme) {
    let area = frame.area();
    let dim = mix_color(theme.background, theme.muted, 18);
    let buffer = frame.buffer_mut();
    for y in area.top()..area.bottom() {
        for x in area.left()..area.right() {
            let cell = &mut buffer[(x, y)];
            cell.set_fg(dim);
        }
    }
}

fn render_overlay(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    if state.overlay.is_some() {
        render_overlay_scrim(frame, theme);
    }
    match state.overlay {
        Some(Overlay::ConfirmQuit) => render_confirm_quit(frame, theme),
        Some(Overlay::Palette) => render_palette(frame, state, theme),
        Some(Overlay::Workspaces) => render_libraries(frame, state, theme),
        Some(Overlay::Help) => render_help(frame, theme),
        Some(Overlay::ConfirmModelDelete) => render_delete_model_confirm(frame, state, theme),
        Some(Overlay::AutomaticStackPreflight) => {
            render_automatic_stack_preflight(frame, state, theme)
        }
        Some(Overlay::AutomaticStackDownloadConfirm) => {
            render_automatic_stack_download_confirmation(frame, state, theme)
        }
        Some(Overlay::FileBrowser) => render_file_browser(frame, state, theme),
        Some(Overlay::ConfirmImport) => {
            render_file_browser(frame, state, theme);
            render_confirm_import(frame, state, theme);
        }
        Some(Overlay::DocumentDetails) => render_document_details(frame, state, theme),
        Some(Overlay::ConfirmDocumentDelete) => render_document_delete(frame, state, theme),
        Some(Overlay::ConfirmLibraryDelete) => render_library_delete(frame, state, theme),
        Some(Overlay::WorkspaceProfile) => render_workspace_profiles(frame, state, theme),
        Some(Overlay::CustomProfileEditor) => render_custom_profile_editor(frame, state, theme),
        Some(Overlay::ChatHistory) => render_chat_history(frame, state, theme),
        Some(Overlay::BookScope) => render_book_scope(frame, state, theme),
        Some(Overlay::DocumentTags) => render_document_tags(frame, state, theme),
        Some(Overlay::CustomModel) => render_custom_model(frame, state, theme),
        None => {}
    }
}

fn render_automatic_stack_preflight(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = centered(82, 30, frame.area());
    frame.render_widget(Clear, area);
    let Some(preflight) = state.automatic_stack_preflight.as_ref() else {
        frame.render_widget(
            Paragraph::new("The automatic stack preview is no longer available. Press Esc.")
                .block(panel("Automatic model stack", true, theme)),
            area,
        );
        return;
    };
    let recommendation = &preflight.recommendation;
    let mut lines = vec![
        Line::styled(
            format!(
                "Tier {} · {} · {} context tokens",
                recommendation.stack_tier, recommendation.profile, recommendation.context_tokens
            ),
            Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
        ),
        Line::styled(
            format!(
                "Catalog {} · recommendation {}",
                nonempty(&recommendation.catalog_release, "release unknown"),
                recommendation.recommendation_id
            ),
            Style::default().fg(theme.muted),
        ),
        Line::from(""),
        Line::styled(
            "Exact model changes",
            Style::default()
                .fg(theme.orange)
                .add_modifier(Modifier::BOLD),
        ),
    ];
    if preflight.changes.is_empty() {
        lines.push(Line::styled(
            "  No model changes",
            Style::default().fg(theme.muted),
        ));
    } else {
        lines.extend(preflight.changes.iter().map(|(role, model)| {
            Line::from(vec![
                Span::styled(format!("  {:<12}", role_label(role)), theme.meta()),
                Span::styled(model.clone(), Style::default().fg(theme.text)),
            ])
        }));
    }
    lines.extend([
        Line::from(""),
        Line::from(vec![
            Span::styled("Download total  ", Style::default().fg(theme.muted)),
            Span::styled(
                format_bytes(recommendation.total_download_bytes),
                Style::default()
                    .fg(if preflight.downloads.is_empty() {
                        theme.green
                    } else {
                        theme.yellow
                    })
                    .add_modifier(Modifier::BOLD),
            ),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Full reindex"), theme.meta()),
            Span::styled(
                if preflight.requires_reindex {
                    "YES — blocked here"
                } else {
                    "No"
                },
                Style::default()
                    .fg(if preflight.requires_reindex {
                        theme.red
                    } else {
                        theme.green
                    })
                    .add_modifier(Modifier::BOLD),
            ),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Media reindex"), theme.meta()),
            Span::styled(
                if preflight.requires_visual_reindex {
                    "Yes"
                } else {
                    "No"
                },
                Style::default().fg(if preflight.requires_visual_reindex {
                    theme.yellow
                } else {
                    theme.green
                }),
            ),
        ]),
    ]);
    for assignment in &preflight.downloads {
        lines.push(Line::styled(
            format!(
                "  {} · {} · {}",
                assignment.role.label(),
                assignment.model,
                format_bytes(assignment.download_bytes)
            ),
            Style::default().fg(theme.yellow),
        ));
    }
    lines.push(Line::from(""));
    if preflight.requires_reindex {
        lines.extend([
            Line::styled(
                "Full rebuild required — nothing will be applied",
                Style::default().fg(theme.red).add_modifier(Modifier::BOLD),
            ),
            Line::styled(
                "The embedding vector space changes. Close this dialog and run the full rebuild workflow.",
                Style::default().fg(theme.red),
            ),
        ]);
    } else if !preflight.can_apply {
        lines.push(Line::styled(
            "This recommendation cannot be applied. No changes will be sent.",
            Style::default().fg(theme.red).add_modifier(Modifier::BOLD),
        ));
    } else if preflight.downloads.is_empty() {
        lines.push(Line::styled(
            "Enter applies these installed models. No download is performed.",
            Style::default().fg(theme.green),
        ));
    } else {
        lines.push(Line::styled(
            "Enter continues to a separate download confirmation. No download happens yet.",
            Style::default().fg(theme.green),
        ));
    }
    for warning in preflight.warnings.iter().take(2) {
        lines.push(Line::styled(
            format!("Warning: {warning}"),
            Style::default().fg(theme.yellow),
        ));
    }
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .block(panel("Use automatic model stack", true, theme)),
        area,
    );
}

fn render_automatic_stack_download_confirmation(
    frame: &mut Frame<'_>,
    state: &AppState,
    theme: &Theme,
) {
    let area = centered(76, 18, frame.area());
    frame.render_widget(Clear, area);
    let Some(preflight) = state.automatic_stack_preflight.as_ref() else {
        return;
    };
    let mut lines = vec![
        Line::from(""),
        Line::styled(
            "Second confirmation · model downloads",
            Style::default().fg(theme.red).add_modifier(Modifier::BOLD),
        ),
        Line::from(""),
        Line::from(vec![
            Span::raw(format!("{} pinned model(s), ", preflight.downloads.len())),
            Span::styled(
                format_bytes(preflight.recommendation.total_download_bytes),
                Style::default()
                    .fg(theme.yellow)
                    .add_modifier(Modifier::BOLD),
            ),
        ]),
    ];
    for assignment in &preflight.downloads {
        lines.push(Line::styled(
            format!(
                "  {} · {} · {}",
                assignment.role.label(),
                assignment.model,
                format_bytes(assignment.download_bytes)
            ),
            Style::default().fg(theme.text),
        ));
    }
    lines.extend([
        Line::from(""),
        Line::styled(
            "Press D to download the models and apply the stack.",
            Style::default().fg(theme.red).add_modifier(Modifier::BOLD),
        ),
        Line::styled(
            "Enter does nothing here. Esc cancels without mutation.",
            Style::default().fg(theme.green),
        ),
    ]);
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .block(panel("Confirm downloads", true, theme)),
        area,
    );
}

fn render_book_scope(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let focused = true;
    let area = centered(
        64,
        (state.documents.len() as u16 + 5).clamp(9, 22),
        frame.area(),
    );
    frame.render_widget(Clear, area);
    let mut items = Vec::with_capacity(state.documents.len() + 1);
    items.push(ListItem::new(Line::styled("All books", theme.title())));
    items.extend(state.documents.iter().map(|document| {
        ListItem::new(Line::styled(
            truncate(&document.title, area.width.saturating_sub(6) as usize),
            theme.body(),
        ))
    }));
    let mut list_state = ListState::default();
    list_state.select(Some(state.chat.scope_cursor.min(state.documents.len())));
    frame.render_stateful_widget(
        selection_list(items, focused, theme).block(panel("Ask across", true, theme)),
        area,
        &mut list_state,
    );
}

fn render_confirm_quit(frame: &mut Frame<'_>, theme: &Theme) {
    let area = confirm_quit_area(frame.area());
    frame.render_widget(Clear, area);
    let block = panel("Quit OmaRag", true, theme);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled("Answers and local data are already saved.", theme.body()),
            Line::from(""),
            Line::from(vec![
                Span::styled("Enter", theme.key()),
                Span::styled("  quit        ", theme.meta()),
                Span::styled("Esc", theme.key()),
                Span::styled("  keep working", theme.meta()),
            ]),
        ]),
        inner,
    );
}

fn render_custom_model(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = centered(68, 12, frame.area());
    frame.render_widget(Clear, area);
    let block = panel("Add custom model", true, theme);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let [intro, mode, input, note, hints] = Layout::vertical([
        Constraint::Length(2),
        Constraint::Length(1),
        Constraint::Length(3),
        Constraint::Fill(1),
        Constraint::Length(1),
    ])
    .areas(inner);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled(
                "Download an Ollama ID or import a local GGUF; Rerank accepts a cross-encoder ID.",
                Style::default().fg(theme.text),
            ),
            Line::styled(
                "Large GGUF files are streamed and validated before Ollama imports them.",
                Style::default().fg(theme.muted),
            ),
        ]),
        intro,
    );
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                "Tab ",
                Style::default()
                    .fg(theme.purple)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                if state.custom_model_file {
                    "Local GGUF"
                } else {
                    "Model ID"
                },
                theme.heading(),
            ),
            Span::styled("   [ ] Role ", Style::default().fg(theme.cyan)),
            Span::styled(state.model_manager.category.label(), theme.title()),
        ])),
        mode,
    );
    render_inline_editor(
        frame,
        input,
        &state.custom_model_input,
        if state.custom_model_file {
            "/path/to/model.gguf"
        } else {
            "qwen3.5:4b"
        },
        true,
        theme,
    );
    frame.render_widget(
        Paragraph::new(
            if state.model_manager.category == omarag_app::ModelCategory::Rerank {
                "A Hugging Face cross-encoder ID becomes the library default and caches on first use; GGUF is blocked."
            } else {
                "Chat, VL and Embedding GGUF files are validated against the selected role."
            },
        )
        .style(Style::default().fg(theme.muted))
        .wrap(Wrap { trim: false }),
        note,
    );
    frame.render_widget(
        Paragraph::new("Enter import · Tab type · [ ] role · Esc cancel")
            .style(Style::default().fg(theme.muted)),
        hints,
    );
}

fn render_file_browser(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let [area, list_area, selected_area, footer] = file_browser_areas(frame.area());
    frame.render_widget(Clear, area);
    frame.render_widget(panel("Add books", true, theme), area);
    render_section_label(
        frame,
        list_area,
        &truncate(
            &state.file_browser.current_dir,
            list_area.width.saturating_sub(2) as usize,
        ),
        theme,
    );
    render_section_label(frame, selected_area, "Selected", theme);

    let entries = state.file_browser.entries.iter().map(|entry| {
        let selected = state
            .file_browser
            .selected
            .iter()
            .any(|path| path == &entry.path);
        ListItem::new(Line::from(vec![
            Span::styled(
                if selected { " [×] " } else { " [ ] " },
                Style::default().fg(if selected { theme.green } else { theme.muted }),
            ),
            Span::styled(if entry.is_dir { "▸ " } else { "  " }, theme.meta()),
            Span::styled(&entry.name, theme.body()),
        ]))
    });
    let mut list_state = ListState::default();
    if !state.file_browser.entries.is_empty() {
        list_state.select(Some(
            state
                .file_browser
                .cursor
                .min(state.file_browser.entries.len() - 1),
        ));
    }
    frame.render_stateful_widget(
        selection_list(entries.collect::<Vec<_>>(), true, theme),
        section_inner(list_area),
        &mut list_state,
    );
    let mut selected = state
        .file_browser
        .selected
        .iter()
        .map(|path| {
            let name = std::path::Path::new(path)
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or(path);
            ListItem::new(Line::from(vec![
                Span::styled(" × ", Style::default().fg(theme.red)),
                Span::styled(
                    truncate(name, selected_area.width.saturating_sub(5) as usize),
                    Style::default().fg(theme.text),
                ),
            ]))
        })
        .collect::<Vec<_>>();
    if selected.is_empty() {
        selected.push(ListItem::new(Line::styled(
            state
                .file_browser
                .error
                .as_deref()
                .unwrap_or(" Space selects folders and PDFs"),
            Style::default().fg(theme.muted),
        )));
    }
    if !state.file_browser.favorites.is_empty() {
        selected.push(ListItem::new(Line::styled(" Favorites", theme.section())));
        selected.extend(state.file_browser.favorites.iter().take(4).map(|path| {
            ListItem::new(Line::styled(
                format!(
                    "   {}",
                    truncate(path, selected_area.width.saturating_sub(4) as usize)
                ),
                Style::default().fg(theme.muted),
            ))
        }));
    }
    if !state.file_browser.history.is_empty() {
        selected.push(ListItem::new(Line::styled(" Recent", theme.section())));
        selected.extend(state.file_browser.history.iter().take(3).map(|path| {
            ListItem::new(Line::styled(
                format!(
                    "   {}",
                    truncate(path, selected_area.width.saturating_sub(4) as usize)
                ),
                Style::default().fg(theme.muted),
            ))
        }));
    }
    frame.render_widget(List::new(selected), section_inner(selected_area));
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled("←→", theme.key()),
            Span::styled(" folder   ", theme.meta()),
            Span::styled("Space", theme.key()),
            Span::styled(" select   ", theme.meta()),
            Span::styled("Enter", theme.key()),
            Span::styled(" review   ", theme.meta()),
            Span::styled("F", theme.key()),
            Span::styled(" favourite   ", theme.meta()),
            Span::styled("R", theme.key()),
            Span::styled(" recent   ", theme.meta()),
            Span::styled("Esc", theme.key()),
            Span::styled(" close", theme.meta()),
        ])),
        footer,
    );
}

fn render_confirm_import(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = confirm_import_area(frame.area());
    frame.render_widget(Clear, area);
    let preflight = &state.library.preflight;
    let eligible = preflight
        .pdfs
        .len()
        .saturating_sub(preflight.unreadable.len())
        .saturating_sub(preflight.encrypted.len());
    let mut lines = vec![
        Line::from(""),
        Line::styled(
            if preflight.busy {
                "Scanning selected folders…".into()
            } else {
                format!("Queue {eligible} readable PDFs for immediate processing?")
            },
            theme.title(),
        ),
        Line::from(vec![
            Span::styled(
                format!("Files {}  ", preflight.pdfs.len()),
                Style::default().fg(theme.cyan),
            ),
            Span::styled(
                format!("Input {}  ", format_bytes(preflight.total_bytes)),
                Style::default().fg(theme.green),
            ),
            Span::styled(
                format!("Index ~{}  ", format_bytes(preflight.estimated_index_bytes)),
                Style::default().fg(theme.purple),
            ),
            Span::styled(
                format!("ETA ~{}", format_duration(preflight.estimated_seconds)),
                Style::default().fg(theme.orange),
            ),
        ]),
        Line::from(vec![
            Span::styled(
                format!("Duplicates {}  ", preflight.duplicates.len()),
                Style::default().fg(theme.yellow),
            ),
            Span::styled(
                format!("Unreadable {}  ", preflight.unreadable.len()),
                Style::default().fg(theme.red),
            ),
            Span::styled(
                format!("Encrypted {}", preflight.encrypted.len()),
                Style::default().fg(theme.red),
            ),
        ]),
        Line::styled(
            format!("Profile: {}", state.active_profile_settings().name),
            Style::default().fg(theme.muted),
        ),
        Line::from(vec![
            Span::styled("Parser  ", Style::default().fg(theme.purple)),
            Span::styled("Docling", Style::default().fg(theme.cyan)),
            Span::raw("  ·  "),
            Span::styled("Chunks  ", Style::default().fg(theme.purple)),
            Span::styled(
                "Hybrid · semantic · ≤384 tokens",
                Style::default().fg(theme.green),
            ),
        ]),
        Line::from(vec![
            Span::styled("Large PDFs  ", Style::default().fg(theme.purple)),
            Span::styled(
                "25-page processing segments · no book limit",
                Style::default().fg(theme.orange),
            ),
        ]),
    ];
    for path in preflight.pdfs.iter().take(3) {
        lines.push(Line::styled(
            format!(
                "  • {}",
                truncate(path, area.width.saturating_sub(8) as usize)
            ),
            Style::default().fg(theme.muted),
        ));
    }
    if !preflight.books.is_empty() {
        lines.push(Line::styled(
            "Confirm the detected book",
            Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
        ));
        for book in preflight.books.iter().take(3) {
            let edition = book
                .metadata
                .edition_label
                .as_deref()
                .unwrap_or("edition unknown");
            let authors = if book.metadata.authors.is_empty() {
                "author unknown".into()
            } else {
                book.metadata.authors.join(", ")
            };
            lines.push(Line::styled(
                format!(
                    "  {} · {} · {}",
                    truncate(&book.metadata.title, 28),
                    truncate(&authors, 20),
                    edition
                ),
                Style::default().fg(theme.text),
            ));
            if !book.issues.is_empty() {
                lines.push(Line::styled(
                    format!("    Check: {}", book.issues.join(" · ")),
                    Style::default().fg(theme.yellow),
                ));
            }
        }
    }
    if let Some(error) = &preflight.error {
        lines.push(Line::styled(error, Style::default().fg(theme.red)));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(vec![
        Span::styled(
            " Enter / Y  Confirm metadata & queue",
            Style::default()
                .fg(theme.orange)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw("      "),
        Span::styled("Esc / N  Back", Style::default().fg(theme.green)),
    ]));
    frame.render_widget(
        Paragraph::new(lines).block(panel("Confirm import", true, theme)),
        area,
    );
}

fn overlay_selected_document(state: &AppState) -> Option<&omarag_domain::DocumentSummary> {
    let index = if state.view == View::Books {
        state.asset_cursor
    } else {
        state.asset_cursor.checked_sub(library_jobs(state).len())?
    };
    library_documents(state).get(index).copied()
}

fn render_document_details(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = centered(70, 26, frame.area());
    frame.render_widget(Clear, area);
    let Some(document) = overlay_selected_document(state) else {
        return;
    };
    let detail = state.library.details.get(&document.id);
    let lines = vec![
        Line::styled(
            &document.title,
            Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
        ),
        Line::from(""),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Status"), theme.meta()),
            Span::raw(&document.status),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Pages"), theme.meta()),
            Span::raw(
                document
                    .page_count
                    .or_else(|| detail.and_then(|item| item.pages))
                    .map_or("scanning".into(), |pages| pages.to_string()),
            ),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Edition"), theme.meta()),
            Span::raw(
                document
                    .book
                    .as_ref()
                    .and_then(|book| book.edition_label.as_deref())
                    .unwrap_or("not confirmed"),
            ),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Authors"), theme.meta()),
            Span::raw(
                document
                    .book
                    .as_ref()
                    .map_or("not confirmed".into(), |book| {
                        if book.authors.is_empty() {
                            "not confirmed".into()
                        } else {
                            book.authors.join(", ")
                        }
                    }),
            ),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "ISBN"), theme.meta()),
            Span::raw(document.book.as_ref().map_or("—".into(), |book| {
                if book.isbn.is_empty() {
                    "—".into()
                } else {
                    book.isbn.join(", ")
                }
            })),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Parser"), theme.meta()),
            Span::raw(format!("{} · structure-aware Hybrid", document.parser_id)),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Conversion"), theme.meta()),
            Span::raw(document.cache_status.as_deref().unwrap_or("legacy")),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Pipeline"), theme.meta()),
            Span::raw(format!(
                "{} OCR · {} text · {} VL pages",
                document
                    .pipeline_stats
                    .get("ocr_pages")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or_default(),
                document
                    .pipeline_stats
                    .get("text_pages")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or_default(),
                document
                    .pipeline_stats
                    .get("vl_pages")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or_default(),
            )),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Size"), theme.meta()),
            Span::raw(detail.map_or("scanning".into(), |item| format_bytes(item.size_bytes))),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Chunks"), theme.meta()),
            Span::raw(document.quality.as_ref().map_or_else(
                || {
                    detail
                        .and_then(|item| item.chunks)
                        .map_or("provided by Haiku".into(), |chunks| chunks.to_string())
                },
                |quality| quality.chunks.to_string(),
            )),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Provenance"), theme.meta()),
            Span::raw(
                document
                    .quality
                    .as_ref()
                    .map_or("unknown".into(), |quality| {
                        format!("{:.0}%", quality.provenance_coverage * 100.0)
                    }),
            ),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Imported"), theme.meta()),
            Span::raw(&document.imported_at),
        ]),
        Line::from(vec![
            Span::styled("Document ID  ", Style::default().fg(theme.muted)),
            Span::raw(&document.id),
        ]),
        Line::from(vec![
            Span::styled("SHA-256      ", Style::default().fg(theme.muted)),
            Span::raw(
                detail
                    .and_then(|item| item.sha256.as_deref())
                    .map_or("scanning".into(), |hash| truncate(hash, 28)),
            ),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Embedding"), theme.meta()),
            Span::raw(
                config_model(
                    state.config.as_ref().map_or("", |config| &config.content),
                    "embeddings",
                )
                .unwrap_or_else(|| "Haiku default".into()),
            ),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "Tags"), theme.meta()),
            Span::raw(
                state
                    .document_tags
                    .get(&document.id)
                    .map_or_else(|| "none".into(), |tags| tags.join(", ")),
            ),
        ]),
        Line::from(""),
        Line::styled(
            truncate(&document.source, area.width.saturating_sub(4) as usize),
            Style::default().fg(theme.yellow),
        ),
        Line::from(""),
        Line::styled(
            "Enter / O open PDF · T edit tags · Esc close",
            Style::default().fg(theme.green),
        ),
    ];
    frame.render_widget(
        Paragraph::new(lines).block(panel("Document details", true, theme)),
        area,
    );
}

fn render_document_delete(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = centered(56, 10, frame.area());
    frame.render_widget(Clear, area);
    let title =
        overlay_selected_document(state).map_or("selected document", |item| item.title.as_str());
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(""),
            Line::styled(
                truncate(title, area.width.saturating_sub(6) as usize),
                Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
            ),
            Line::from(""),
            Line::styled(
                "Remove this document from the Haiku index?",
                Style::default().fg(theme.text),
            ),
            Line::styled(
                "The original PDF is never deleted. Ctrl+Z restores it.",
                Style::default().fg(theme.muted),
            ),
            Line::from(""),
            Line::from(vec![
                Span::styled(
                    " Enter / Y Remove",
                    Style::default().fg(theme.red).add_modifier(Modifier::BOLD),
                ),
                Span::raw("       "),
                Span::styled("Esc / N Cancel", Style::default().fg(theme.green)),
            ]),
        ])
        .block(panel("Safe removal", true, theme)),
        area,
    );
}

fn render_workspace_profiles(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = centered(78, 20, frame.area());
    frame.render_widget(Clear, area);
    let block = panel("Library profiles", true, theme);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let [body, footer] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(2)]).areas(inner);
    let [list, details] =
        Layout::horizontal([Constraint::Percentage(42), Constraint::Percentage(58)]).areas(body);
    let active = state.active_profile_settings();
    let items = (0..state.profile_count())
        .map(|index| {
            let profile = state.profile_settings_at(index);
            let selected = profile.id == active.id;
            ListItem::new(Line::from(vec![
                Span::styled(
                    if selected { " ● " } else { " ○ " },
                    Style::default().fg(if selected { theme.green } else { theme.muted }),
                ),
                Span::styled(profile.name, Style::default().fg(theme.text)),
                Span::styled(
                    if index < WorkspaceProfile::ALL.len() {
                        "  built-in"
                    } else {
                        "  custom"
                    },
                    Style::default().fg(theme.muted),
                ),
            ]))
        })
        .collect::<Vec<_>>();
    let mut list_state = ListState::default();
    list_state.select(Some(
        state.profile_cursor.min(items.len().saturating_sub(1)),
    ));
    frame.render_stateful_widget(selection_list(items, true, theme), list, &mut list_state);
    let selected = state.profile_settings_at(state.profile_cursor);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled(
                selected.name,
                Style::default()
                    .fg(theme.orange)
                    .add_modifier(Modifier::BOLD),
            ),
            Line::from(""),
            profile_setting_line("Pipeline", &selected.processing_profile, theme),
            profile_setting_line("Duplicates", &selected.duplicate_policy, theme),
            profile_setting_line("Validity", &selected.validity_policy, theme),
            Line::from(""),
            Line::styled(
                "These values are applied to every new import in this library.",
                Style::default().fg(theme.muted),
            ),
        ])
        .wrap(Wrap { trim: false }),
        details,
    );
    frame.render_widget(
        Paragraph::new(vec![
            shortcut_words(
                theme,
                &[
                    ("Apply", 'A'),
                    ("Custom", 'C'),
                    ("Edit", 'E'),
                    ("Back", 'B'),
                ],
            ),
            Line::styled(
                "Enter/A apply   C new custom   E edit custom   Esc/B back",
                Style::default().fg(theme.muted),
            ),
        ]),
        footer,
    );
}

fn profile_setting_line(label: &str, value: &str, theme: &Theme) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("{label:<12}"), Style::default().fg(theme.muted)),
        Span::styled(
            value.to_owned(),
            Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
        ),
    ])
}

fn render_custom_profile_editor(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = centered(66, 18, frame.area());
    frame.render_widget(Clear, area);
    let block = panel("Custom library profile", true, theme);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let [intro, fields, footer] = Layout::vertical([
        Constraint::Length(2),
        Constraint::Fill(1),
        Constraint::Length(3),
    ])
    .areas(inner);
    frame.render_widget(
        Paragraph::new("Tab selects a setting. Left/right changes its value."),
        intro,
    );
    let values = [
        state.custom_profile_name.value.as_str(),
        state.custom_profile_draft.processing_profile.as_str(),
        state.custom_profile_draft.duplicate_policy.as_str(),
        state.custom_profile_draft.validity_policy.as_str(),
    ];
    let labels = ["Name", "Pipeline", "Duplicates", "Validity"];
    let lines = labels
        .iter()
        .zip(values)
        .enumerate()
        .map(|(index, (label, value))| {
            let focused = index == state.custom_profile_field;
            Line::from(vec![
                Span::styled(
                    format!("{} {label:<12}", if focused { "›" } else { " " }),
                    Style::default().fg(if focused { theme.orange } else { theme.muted }),
                ),
                Span::styled(
                    format!(" {value} "),
                    Style::default()
                        .fg(if focused {
                            theme.background
                        } else {
                            theme.text
                        })
                        .bg(if focused { theme.focus } else { theme.panel })
                        .add_modifier(Modifier::BOLD),
                ),
            ])
        })
        .collect::<Vec<_>>();
    frame.render_widget(Paragraph::new(lines), fields);
    frame.render_widget(
        Paragraph::new(vec![
            shortcut_words(theme, &[("Save", 'S'), ("Cancel", 'C')]),
            Line::styled(
                "Enter/Ctrl+S save   Esc cancel",
                Style::default().fg(theme.muted),
            ),
        ]),
        footer,
    );
}

fn render_library_delete(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = centered(62, 12, frame.area());
    frame.render_widget(Clear, area);
    let library = state
        .workspaces
        .get(state.workspace_cursor)
        .map_or("selected library", |item| item.name.as_str());
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(""),
            Line::styled(
                library.to_owned(),
                Style::default()
                    .fg(theme.orange)
                    .add_modifier(Modifier::BOLD),
            ),
            Line::from(""),
            Line::styled(
                "Enter removes the library from OmaRag but keeps every file.",
                Style::default().fg(theme.text),
            ),
            Line::styled(
                "Shift+D permanently removes its local index and library directory.",
                Style::default().fg(theme.red),
            ),
            Line::from(""),
            shortcut_words(
                theme,
                &[
                    ("Unregister", 'U'),
                    ("Delete permanently", 'D'),
                    ("Cancel", 'C'),
                ],
            ),
        ])
        .wrap(Wrap { trim: false })
        .block(panel("Delete library?", true, theme).border_style(Style::default().fg(theme.red))),
        area,
    );
}

fn render_chat_history(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = centered(72, 22, frame.area());
    frame.render_widget(Clear, area);
    let sessions = state
        .active_workspace
        .as_ref()
        .and_then(|workspace| state.chat_sessions.get(workspace))
        .map_or(&[][..], Vec::as_slice);
    let mut items = sessions
        .iter()
        .map(|session| {
            ListItem::new(vec![
                Line::styled(
                    truncate(&session.question, area.width.saturating_sub(8) as usize),
                    Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
                ),
                Line::styled(
                    format!(
                        "{} · {} citations",
                        session.created_at,
                        session.citations.len()
                    ),
                    Style::default().fg(theme.muted),
                ),
            ])
        })
        .collect::<Vec<_>>();
    if items.is_empty() {
        items.push(ListItem::new(Line::styled(
            "No saved conversations yet. Ask a question first.",
            Style::default().fg(theme.muted),
        )));
    }
    let mut list_state = ListState::default();
    if !sessions.is_empty() {
        list_state.select(Some(state.history_cursor.min(sessions.len() - 1)));
    }
    frame.render_stateful_widget(
        List::new(items)
            .highlight_symbol(Theme::selection_marker(true))
            .highlight_style(Style::default().bg(theme.selection))
            .block(panel(
                "Chat history · Enter restore · R rerun · E edit · X export",
                true,
                theme,
            )),
        area,
        &mut list_state,
    );
}

fn render_document_tags(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = centered(58, 9, frame.area());
    frame.render_widget(Clear, area);
    let inner = panel("Document tags", true, theme).inner(area);
    frame.render_widget(panel("Document tags", true, theme), area);
    let [intro, editor, footer] = Layout::vertical([
        Constraint::Length(2),
        Constraint::Length(3),
        Constraint::Fill(1),
    ])
    .areas(inner);
    frame.render_widget(
        Paragraph::new("Comma-separated tags are local to OmaRag and searchable with /."),
        intro,
    );
    render_inline_editor(frame, editor, &state.tag_editor, "Tags", true, theme);
    frame.render_widget(Paragraph::new("Enter save · Esc cancel"), footer);
}

fn render_delete_model_confirm(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = delete_model_confirm_area(frame.area());
    frame.render_widget(Clear, area);
    let model = state
        .model_manager
        .delete_candidate
        .as_deref()
        .unwrap_or("selected model");
    let content = Text::from(vec![
        Line::from(""),
        Line::styled(
            truncate(model, area.width.saturating_sub(6) as usize),
            Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
        ),
        Line::styled(
            "This permanently removes its local Ollama data.",
            Style::default().fg(theme.muted),
        ),
        Line::from(""),
        Line::from(vec![
            Span::styled(
                "  Enter / Y  Delete",
                Style::default().fg(theme.red).add_modifier(Modifier::BOLD),
            ),
            Span::raw("        "),
            Span::styled("Esc / N  Cancel  ", Style::default().fg(theme.green)),
        ]),
    ]);
    frame.render_widget(
        Paragraph::new(content).block(
            panel("Delete local model?", true, theme).border_style(Style::default().fg(theme.red)),
        ),
        area,
    );
}

fn package_details(package: &ModelPackage, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::from(vec![
            Span::styled(
                format!("Stack #{}  {}", package.recommended_rank, package.name),
                Style::default()
                    .fg(theme.orange)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                format!(
                    "  {} · {}",
                    package.fit.label(),
                    human_memory(package.total_estimated_memory)
                ),
                Style::default().fg(match package.fit {
                    ModelFit::Comfortable => theme.green,
                    ModelFit::Tight => theme.yellow,
                }),
            ),
        ]),
        Line::styled(package.summary.clone(), Style::default().fg(theme.text)),
        Line::styled(
            format!("↳ {}", package.synergy),
            Style::default().fg(theme.purple),
        ),
    ];
    lines.extend(package.models.iter().map(|model| {
        Line::from(vec![
            Span::styled(
                format!(" {:<10}", model.role.label()),
                Style::default().fg(theme.muted),
            ),
            Span::styled(
                if model.installed { "✓ " } else { "○ " },
                Style::default().fg(if model.installed {
                    theme.green
                } else {
                    theme.muted
                }),
            ),
            Span::styled(model.model.clone(), Style::default().fg(theme.cyan)),
        ])
    }));
    if package
        .models
        .iter()
        .any(|model| model.source == ModelSource::HuggingFace && !model.installed)
    {
        lines.push(Line::from(""));
        lines.push(Line::styled(
            "The pinned cross-encoder will be downloaded and verified with this package.",
            Style::default().fg(theme.orange),
        ));
    }
    lines
}

fn model_details(
    entry: &ModelCatalogEntry,
    state: &AppState,
    metrics: &RuntimeMetrics,
    theme: &Theme,
) -> Text<'static> {
    let loaded = metrics
        .loaded_models
        .iter()
        .any(|model| model_matches(&model.name, &entry.id));
    let estimate = entry.estimated_memory;
    let (fit, fit_color) = match entry.fit {
        ModelFit::Comfortable => (entry.fit.label(), theme.green),
        ModelFit::Tight => (entry.fit.label(), theme.yellow),
    };
    let target = match entry.source {
        ModelSource::Installed => entry.id.clone(),
        ModelSource::Ollama => format!(
            "{}-{}",
            entry.id,
            state.model_manager.quantization.ollama_label()
        ),
        ModelSource::HuggingFace => format!(
            "hf.co/{}:{}",
            entry.id,
            state.model_manager.quantization.label()
        ),
    };
    Text::from(vec![
        Line::styled(
            entry.id.clone(),
            Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
        ),
        Line::from(vec![
            Span::styled(entry.source.label(), Style::default().fg(theme.purple)),
            Span::styled(
                format!("  {}", entry.category.label()),
                Style::default().fg(theme.orange),
            ),
            Span::styled(
                if loaded {
                    "  ● loaded"
                } else if entry.installed {
                    "  ✓ installed"
                } else {
                    "  ○ remote"
                },
                Style::default().fg(if loaded { theme.green } else { theme.muted }),
            ),
        ]),
        Line::from(""),
        Line::from(entry.description.clone()),
        Line::styled(
            entry.recommended_rank.map_or_else(
                || "Hardware compatible".into(),
                |rank| format!("TOP {rank} recommendation for this profile"),
            ),
            Style::default()
                .fg(theme.orange)
                .add_modifier(Modifier::BOLD),
        ),
        Line::from(""),
        Line::from(vec![
            Span::styled("♥ ", Style::default().fg(theme.red)),
            Span::raw(entry.likes.map_or_else(|| "—".into(), format_count)),
            Span::raw("    "),
            Span::styled("↓ ", Style::default().fg(theme.green)),
            Span::raw(entry.downloads.map_or_else(|| "—".into(), format_count)),
        ]),
        Line::from(vec![
            Span::styled("Estimate  ", Style::default().fg(theme.muted)),
            Span::styled(human_memory(estimate), Style::default().fg(theme.text)),
            Span::raw(" incl. context reserve"),
        ]),
        Line::from(vec![
            Span::styled("Hardware  ", Style::default().fg(theme.muted)),
            Span::styled(
                fit,
                Style::default().fg(fit_color).add_modifier(Modifier::BOLD),
            ),
        ]),
        Line::from(vec![
            Span::styled(format!("{:<LABEL_COLUMN$}", "GPU"), theme.meta()),
            Span::styled(
                format!(
                    "{} · VRAM {} · shared {}",
                    metrics.gpu_name.as_deref().unwrap_or("unknown"),
                    human_memory(metrics.vram_total),
                    human_memory(metrics.shared_gpu_memory)
                ),
                Style::default().fg(theme.text),
            ),
        ]),
        Line::from(""),
        Line::styled(
            format!("Target: {target}"),
            Style::default().fg(theme.yellow),
        ),
        Line::styled(
            if entry.source == ModelSource::HuggingFace {
                "HF import uses Ollama's hf.co bridge; sharded or unsupported GGUFs may fail."
            } else {
                "Downloads stay cold. Load is explicit and uses the selected expiry policy."
            },
            Style::default().fg(theme.muted),
        ),
    ])
}

/// Command palette. One modal surface: the query, a rule, then the matches.
fn render_palette(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let commands = filtered_palette_commands(state);
    let area = centered(64, (commands.len() as u16 + 4).clamp(9, 21), frame.area());
    frame.render_widget(Clear, area);
    let block = panel("Commands", true, theme);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.height < 3 {
        return;
    }
    let [query, rule, list] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Fill(1),
    ])
    .areas(inner);

    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled("› ", Style::default().fg(theme.focus)),
            Span::styled(state.palette.query.value.clone(), theme.body()),
            Span::styled(
                if state.palette.query.value.is_empty() {
                    "type to filter"
                } else {
                    ""
                },
                theme.meta(),
            ),
        ])),
        query,
    );
    frame.render_widget(
        Paragraph::new(Line::styled("─".repeat(rule.width as usize), theme.rule())),
        rule,
    );

    let items = commands
        .iter()
        .map(|command| ListItem::new(format!(" {}", command.label())))
        .collect::<Vec<_>>();
    let mut list_state = ListState::default();
    if !commands.is_empty() {
        list_state.select(Some(state.palette.cursor.min(commands.len() - 1)));
    }
    frame.render_stateful_widget(selection_list(items, true, theme), list, &mut list_state);
}

fn render_libraries(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    if state.creating_workspace {
        let area = centered(66, 16, frame.area());
        frame.render_widget(Clear, area);
        let inner = panel("New library", true, theme).inner(area);
        frame.render_widget(panel("New library", true, theme), area);
        let [intro, editor, profile, actions] = Layout::vertical([
            Constraint::Length(2),
            Constraint::Length(3),
            Constraint::Length(6),
            Constraint::Fill(1),
        ])
        .areas(inner);
        frame.render_widget(
            Paragraph::new("Create an isolated local evidence collection."),
            intro,
        );
        render_inline_editor(
            frame,
            editor,
            &state.workspace_name,
            "Library name",
            true,
            theme,
        );
        let selected = state.profile_settings_at(state.profile_cursor);
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(vec![
                    Span::styled("Profile  ", Style::default().fg(theme.muted)),
                    Span::styled(
                        selected.name,
                        Style::default()
                            .fg(theme.orange)
                            .add_modifier(Modifier::BOLD),
                    ),
                ]),
                profile_setting_line("Pipeline", &selected.processing_profile, theme),
                profile_setting_line("Duplicates", &selected.duplicate_policy, theme),
                profile_setting_line("Validity", &selected.validity_policy, theme),
                Line::from(vec![
                    Span::styled("Alt+P", theme.key()),
                    Span::styled(" profiles   ", theme.meta()),
                    Span::styled("Alt+C", theme.key()),
                    Span::styled(" custom", theme.meta()),
                ]),
            ]),
            profile,
        );
        frame.render_widget(
            Paragraph::new(vec![Line::from(vec![
                Span::styled(
                    "Enter",
                    Style::default()
                        .fg(theme.green)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw(" create   "),
                Span::styled(
                    "Tab",
                    Style::default()
                        .fg(theme.orange)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw(" next profile   "),
                Span::styled(
                    "Esc",
                    Style::default().fg(theme.red).add_modifier(Modifier::BOLD),
                ),
                Span::raw(" back"),
            ])]),
            actions,
        );
        return;
    }
    let area = centered(
        58,
        (state.workspaces.len() as u16 + 7).clamp(10, 21),
        frame.area(),
    );
    frame.render_widget(Clear, area);
    let mut items = state
        .workspaces
        .iter()
        .map(|workspace| {
            let active = state.active_workspace.as_ref() == Some(&workspace.id);
            ListItem::new(Line::from(vec![
                Span::styled(
                    if active { "● " } else { "○ " },
                    Style::default().fg(if active { theme.green } else { theme.muted }),
                ),
                Span::styled(&workspace.name, theme.title()),
                Span::styled(
                    format!("  {}", workspace.path),
                    Style::default().fg(theme.muted),
                ),
            ]))
        })
        .collect::<Vec<_>>();
    items.push(ListItem::new(""));
    items.push(ListItem::new(shortcut_words(
        theme,
        &[("New library", 'N')],
    )));
    items.push(ListItem::new(shortcut_words(
        theme,
        &[("Delete selected library", 'D')],
    )));
    let mut list_state = ListState::default();
    if !state.workspaces.is_empty() {
        list_state.select(Some(state.workspace_cursor));
    }
    frame.render_stateful_widget(
        List::new(items)
            .highlight_symbol(Theme::selection_marker(true))
            .highlight_style(theme.selection_style(true))
            .block(panel(
                "Libraries · arrows · Enter · N new · D delete",
                true,
                theme,
            )),
        area,
        &mut list_state,
    );
}

/// Keyboard reference. Built from a table so the key column aligns by
/// construction instead of by hand-counted spaces.
fn render_help(frame: &mut Frame<'_>, theme: &Theme) {
    const GROUPS: [(&str, &[(&str, &str)]); 4] = [
        (
            "Move",
            &[
                ("Tab / Shift+Tab", "next / previous pane"),
                ("↑ ↓ ← →", "move within the focused pane"),
                ("Enter", "open, edit, or pause/resume"),
                ("Esc", "leave text input or close"),
            ],
        ),
        (
            "Go to",
            &[
                ("Ctrl+H", "model presets"),
                ("Ctrl+K", "model catalog"),
                ("Ctrl+A", "indexing"),
                ("Ctrl+S", "sources"),
                ("Ctrl+L", "libraries"),
                (":  Ctrl+P", "command palette"),
            ],
        ),
        (
            "Do",
            &[
                ("Ctrl+O", "add PDFs"),
                ("N", "new library"),
                ("Ctrl+E", "evidence mode"),
                ("Ctrl+R", "refresh"),
                ("Ctrl+X", "stop the active run"),
                ("Ctrl+Z", "undo"),
                ("Ctrl+T", "next theme"),
                ("/", "search in the current view"),
            ],
        ),
        (
            "Mouse",
            &[
                ("click / wheel", "focus, activate, scroll"),
                ("drag", "select answer text to copy"),
                ("right / middle", "back / cycle theme"),
            ],
        ),
    ];

    let mut lines = Vec::new();
    for (index, (title, entries)) in GROUPS.iter().enumerate() {
        if index > 0 {
            lines.push(Line::raw(""));
        }
        lines.push(Line::styled(*title, theme.section()));
        for (key, description) in entries.iter() {
            lines.push(Line::from(vec![
                Span::styled(format!("  {key:<16}"), theme.key()),
                Span::styled(*description, theme.body()),
            ]));
        }
    }
    lines.push(Line::raw(""));
    lines.push(Line::from(vec![
        Span::styled("  Esc", theme.key()),
        Span::styled("  close this help · ", theme.meta()),
        Span::styled("Ctrl+C", theme.key()),
        Span::styled("  quit", theme.meta()),
    ]));

    let height = (lines.len() as u16 + 2).min(frame.area().height.saturating_sub(2));
    let area = centered(56, height, frame.area());
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(lines)
            .style(Style::default().bg(theme.panel).fg(theme.text))
            .block(panel("Keyboard", true, theme)),
        area,
    );
}

fn render_inline_editor(
    frame: &mut Frame<'_>,
    area: Rect,
    editor: &EditorState,
    label: &str,
    focused: bool,
    theme: &Theme,
) {
    let block = if label.is_empty() {
        Block::default()
    } else {
        Block::default()
            .borders(Borders::TOP)
            .title(format!("─┤ {label} ├"))
            .title_style(Style::default().fg(if focused {
                theme.workspace.border_active
            } else {
                theme.muted
            }))
            .border_style(Style::default().fg(if focused {
                theme.workspace.border_active
            } else {
                theme.workspace.border
            }))
    };
    let lines = editor
        .value
        .split('\n')
        .map(str::to_owned)
        .collect::<Vec<_>>();
    let mut textarea = TextArea::new(if lines.is_empty() {
        vec![String::new()]
    } else {
        lines
    });
    let before = &editor.value[..editor.cursor.min(editor.value.len())];
    let row = before.bytes().filter(|byte| *byte == b'\n').count();
    let column = before
        .rsplit('\n')
        .next()
        .unwrap_or_default()
        .chars()
        .count();
    textarea.move_cursor(CursorMove::Jump(row as u16, column as u16));
    textarea.set_block(block);
    textarea.set_style(Style::default().fg(if focused { theme.text } else { theme.muted }));
    textarea.set_cursor_line_style(Style::default());
    textarea.set_cursor_style(if focused {
        Style::default().fg(theme.background).bg(theme.yellow)
    } else {
        Style::default().fg(theme.muted)
    });
    frame.render_widget(&textarea, area);
}

/// A modal surface, in the same frame language as the panes: rounded corners
/// and the title cut into the top edge.
/// A modal surface in the same frame language as the panes: rounded corners and
/// the title cut into the top edge, `╭─┤ Title ├───╮`. The leading rule matches
/// `render_frame`, so a dialog and a pane are visibly the same system.
fn panel<'a>(title: &'a str, focused: bool, theme: &Theme) -> Block<'a> {
    panel_with_status(title, &[], focused, theme)
}

/// A modal that also carries status in its bottom edge, the way panes do.
fn panel_with_status<'a>(
    title: &'a str,
    status: &[String],
    focused: bool,
    theme: &Theme,
) -> Block<'a> {
    let mut block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .title(format!("─┤ {title} ├"))
        .title_style(if focused {
            Style::default()
                .fg(theme.modal.border_active)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default()
                .fg(theme.muted)
                .add_modifier(Modifier::BOLD)
        })
        .border_style(Style::default().fg(if focused {
            theme.modal.border_active
        } else {
            theme.workspace.border
        }))
        .style(Style::default().bg(theme.modal.bg).fg(theme.modal.fg));
    for item in status.iter().filter(|item| !item.is_empty()) {
        block = block.title_bottom(Line::styled(format!("┤ {item} ├─"), theme.meta()));
    }
    block
}

/// One indexing run: state, name, a gradient bar, and whatever the backend can
/// actually say about how far along it is.
fn activity_item<'a>(
    job: &'a JobSnapshot,
    width: u16,
    set: IconSet,
    theme: &Theme,
) -> ListItem<'a> {
    let color = job_color(job, theme);
    let running = !is_terminal(&job.status);

    // Reserve room for the marker, the percentage and a little air.
    let bar_width = width.saturating_sub(30).clamp(8, 32);
    let mut headline = vec![
        Span::styled(
            format!(" {} ", status_symbol(&job.status, set)),
            Style::default().fg(color),
        ),
        Span::styled(&job.kind, theme.title()),
        Span::raw("  "),
    ];
    if running {
        headline.extend(progress::bar_with(
            bar_width,
            job.progress,
            theme.gradient[0],
            theme.gradient[1],
            theme.muted,
        ));
        headline.push(Span::styled(
            format!(" {:>3.0}%", job.progress.clamp(0.0, 1.0) * 100.0),
            Style::default().fg(color),
        ));
    } else {
        headline.push(Span::styled(
            job_state_label(&job.status),
            Style::default().fg(color),
        ));
    }

    ListItem::new(vec![
        Line::from(headline),
        Line::styled(
            format!(
                "   {}",
                truncate(&job_detail(job), width.saturating_sub(6) as usize)
            ),
            Style::default().fg(theme.muted),
        ),
    ])
}

/// The real ingest phases, in the order the bridge emits them.
///
/// These mirror the label map in `services/job_service.py`; the strip used to
/// show an invented sequence that matched nothing the backend ever reported.
const INGEST_PHASES: [(&str, &str); 8] = [
    ("archiving", "archive"),
    ("profiling", "profile"),
    ("converting", "convert"),
    ("reconciling", "structure"),
    ("chunking", "chunk"),
    ("embedding", "embed"),
    ("committing", "commit"),
    ("verifying", "verify"),
];

/// The pipeline as a live strip: done phases dimmed, the running one accented.
fn pipeline_strip(state: &AppState, width: u16, theme: &Theme) -> Line<'static> {
    let current = state
        .jobs
        .values()
        .find(|job| !is_terminal(&job.status))
        .map(|job| job.phase.to_ascii_lowercase());
    let reached = current.as_ref().and_then(|phase| {
        INGEST_PHASES
            .iter()
            .position(|(key, _)| phase.contains(key))
    });

    // The full strip needs the labels plus three columns per separator.
    let full_width: usize = INGEST_PHASES
        .iter()
        .map(|(_, label)| label.len())
        .sum::<usize>()
        + (INGEST_PHASES.len() - 1) * 3;
    if full_width > width as usize {
        // Too narrow for the whole pipeline: name the current step and its
        // position instead of truncating the sequence mid-word.
        return match reached {
            Some(active) => Line::from(vec![
                Span::styled(INGEST_PHASES[active].1, theme.status(StatusLevel::Busy)),
                Span::styled(
                    format!("  step {} of {}", active + 1, INGEST_PHASES.len()),
                    theme.meta(),
                ),
            ]),
            None => Line::styled(
                format!("{} phases · archive to verify", INGEST_PHASES.len()),
                theme.meta(),
            ),
        };
    }

    let mut spans = Vec::with_capacity(INGEST_PHASES.len() * 2);
    for (index, (_, label)) in INGEST_PHASES.iter().enumerate() {
        if index > 0 {
            spans.push(Span::styled(" › ", theme.rule()));
        }
        let style = match reached {
            Some(active) if index == active => theme.status(StatusLevel::Busy),
            Some(active) if index < active => theme.meta(),
            Some(_) => Style::default().fg(theme.border),
            None => theme.meta(),
        };
        spans.push(Span::styled(*label, style));
    }
    Line::from(spans)
}

/// Terminal jobs say what happened rather than showing a spent bar.
fn job_state_label(status: &JobStatus) -> &'static str {
    match status {
        JobStatus::Completed => "done",
        JobStatus::Failed => "failed",
        JobStatus::Cancelled => "cancelled",
        JobStatus::Paused => "paused",
        JobStatus::PauseRequested => "pausing",
        JobStatus::Queued => "queued",
        JobStatus::Running => "running",
    }
}

/// The phase, plus the page range and estimate the backend already reports and
/// that used to be visible only in the inspector.
fn job_detail(job: &JobSnapshot) -> String {
    let mut detail = job.phase.clone();
    if let Some(progress) = job.progress_detail.as_ref() {
        if let (Some(start), Some(end)) = (progress.page_start, progress.page_end) {
            match progress.total_pages {
                Some(total) if total > 0 => {
                    detail.push_str(&format!(" · pages {start}–{end} of {total}"));
                }
                _ => detail.push_str(&format!(" · pages {start}–{end}")),
            }
        }
        if let (Some(low), Some(high)) = (progress.eta_seconds_low, progress.eta_seconds_high) {
            detail.push_str(&format!(
                " · {}–{} left",
                format_duration(low.round() as u64),
                format_duration(high.round() as u64)
            ));
        }
    }
    detail
}

pub(crate) fn configured_models(state: &AppState) -> Vec<(String, String)> {
    let content = state
        .config
        .as_ref()
        .map_or("", |config| config.content.as_str());
    let chat = config_section_value(content, "model_defaults", "chat")
        .or_else(|| config_model(content, "qa"))
        .unwrap_or_else(|| "not configured".into());
    let vision = config_section_value(content, "model_defaults", "vl").unwrap_or_else(|| {
        if config_flag(content, "qa", "vision") {
            chat.clone()
        } else {
            config_model_after(content, "picture_description")
                .unwrap_or_else(|| "not configured".into())
        }
    });
    vec![
        ("Chat".into(), chat),
        ("VL".into(), vision),
        (
            "Embedding".into(),
            config_section_value(content, "model_defaults", "embedding")
                .or_else(|| config_model(content, "embeddings"))
                .unwrap_or_else(|| "not configured".into()),
        ),
        (
            "Rerank".into(),
            config_section_value(content, "model_defaults", "rerank")
                .or_else(|| config_model(content, "reranking"))
                .unwrap_or_else(|| "not configured".into()),
        ),
    ]
}

pub(crate) fn configured_vector_dim(state: &AppState) -> u32 {
    let content = state
        .config
        .as_ref()
        .map_or("", |config| config.content.as_str());
    let mut in_embeddings = false;
    for line in content.lines() {
        if !line.starts_with(char::is_whitespace) {
            in_embeddings = line.trim_end_matches(':') == "embeddings";
            continue;
        }
        if in_embeddings
            && let Some(value) = line.trim().strip_prefix("vector_dim:")
            && let Ok(dimension) = value.trim().parse()
        {
            return dimension;
        }
    }
    1024
}

fn config_section_value(content: &str, marker: &str, key: &str) -> Option<String> {
    let lines = content.lines().collect::<Vec<_>>();
    let start = lines
        .iter()
        .position(|line| line.trim_end().trim_end_matches(':').trim() == marker)?;
    let marker_indent = lines[start].len() - lines[start].trim_start().len();
    for line in lines.into_iter().skip(start + 1) {
        let indent = line.len() - line.trim_start().len();
        if !line.trim().is_empty() && indent <= marker_indent {
            break;
        }
        if let Some(value) = line.trim().strip_prefix(&format!("{key}:")) {
            return Some(value.trim().trim_matches(['\'', '"']).to_owned());
        }
    }
    None
}

fn config_flag(content: &str, wanted_section: &str, wanted_key: &str) -> bool {
    let mut in_section = false;
    for line in content.lines() {
        if !line.starts_with(char::is_whitespace) {
            in_section = line.trim_end_matches(':') == wanted_section;
            continue;
        }
        if in_section
            && line
                .trim()
                .strip_prefix(&format!("{wanted_key}:"))
                .is_some_and(|value| value.trim().eq_ignore_ascii_case("true"))
        {
            return true;
        }
    }
    false
}

fn config_model_after(content: &str, marker: &str) -> Option<String> {
    let lines = content.lines().collect::<Vec<_>>();
    let start = lines.iter().position(|line| {
        line.trim()
            .strip_suffix(':')
            .is_some_and(|value| value == marker)
    })?;
    let marker_indent = lines[start].len() - lines[start].trim_start().len();
    for line in lines.into_iter().skip(start + 1) {
        let indent = line.len() - line.trim_start().len();
        if !line.trim().is_empty() && indent <= marker_indent {
            break;
        }
        if let Some(name) = line.trim().strip_prefix("name:") {
            return Some(name.trim().trim_matches(['\'', '"']).to_owned());
        }
    }
    None
}

fn config_model(content: &str, wanted_section: &str) -> Option<String> {
    let mut in_section = false;
    for line in content.lines() {
        if !line.starts_with(char::is_whitespace) {
            in_section = line.trim_end_matches(':') == wanted_section;
            continue;
        }
        if !in_section {
            continue;
        }
        let trimmed = line.trim();
        if trimmed == "model: null" {
            return None;
        }
        if let Some(name) = trimmed.strip_prefix("name:") {
            return Some(name.trim().trim_matches(['\'', '"']).to_owned());
        }
    }
    None
}

fn model_matches(loaded: &str, configured: &str) -> bool {
    loaded == configured
        || loaded.trim_end_matches(":latest") == configured.trim_end_matches(":latest")
}

fn compact_model_name(model: &str) -> String {
    if model == "not configured" {
        return "unset".into();
    }
    model
        .rsplit('/')
        .next()
        .unwrap_or(model)
        .trim_end_matches(":latest")
        .to_owned()
}

fn job_color(job: &JobSnapshot, theme: &Theme) -> Color {
    match job.status {
        JobStatus::Completed => theme.green,
        JobStatus::Failed => theme.red,
        JobStatus::Paused | JobStatus::PauseRequested => theme.yellow,
        JobStatus::Cancelled => theme.muted,
        JobStatus::Queued | JobStatus::Running => theme.cyan,
    }
}

/// The mark in front of a run. Always present — a job list without a state
/// marker is unreadable — so this follows the icon *set* but ignores the mode.
fn status_symbol(status: &JobStatus, set: IconSet) -> &'static str {
    icons::job_glyph(
        match status {
            JobStatus::Queued => Icon::Queued,
            JobStatus::Running => Icon::Running,
            JobStatus::PauseRequested | JobStatus::Paused => Icon::Paused,
            JobStatus::Completed => Icon::Done,
            JobStatus::Cancelled | JobStatus::Failed => Icon::Failed,
        },
        set,
    )
}

fn is_terminal(status: &JobStatus) -> bool {
    matches!(
        status,
        JobStatus::Paused | JobStatus::Completed | JobStatus::Cancelled | JobStatus::Failed
    )
}
fn spinner(tick: u64) -> &'static str {
    ["◐", "◓", "◑", "◒"][(tick as usize / 2) % 4]
}

/// Sizes use a non-breaking space so a wrapping paragraph can never leave the
/// number on one line and its unit on the next.
fn human_memory(bytes: u64) -> String {
    const GIB: f64 = 1_073_741_824.0;
    const MIB: f64 = 1_048_576.0;
    if bytes as f64 >= GIB {
        format!("{:.1}\u{a0}GiB", bytes as f64 / GIB)
    } else {
        format!("{:.0}\u{a0}MiB", bytes as f64 / MIB)
    }
}

fn compact_memory(bytes: u64) -> String {
    const GIB: f64 = 1_073_741_824.0;
    const MIB: f64 = 1_048_576.0;
    if bytes as f64 >= GIB {
        format!("{:.1}G", bytes as f64 / GIB)
    } else {
        format!("{:.0}M", bytes as f64 / MIB)
    }
}

fn format_bytes(bytes: u64) -> String {
    if bytes < 1_048_576 {
        format!("{:.0}\u{a0}KiB", bytes as f64 / 1024.0)
    } else {
        human_memory(bytes)
    }
}

fn format_duration(seconds: u64) -> String {
    if seconds < 60 {
        format!("{seconds}s")
    } else {
        format!("{}m {:02}s", seconds / 60, seconds % 60)
    }
}

fn format_count(value: u64) -> String {
    if value >= 1_000_000 {
        format!("{:.1}M", value as f64 / 1_000_000.0)
    } else if value >= 1_000 {
        format!("{:.1}K", value as f64 / 1_000.0)
    } else {
        value.to_string()
    }
}

#[cfg(test)]
fn estimated_model_memory(
    entry: &ModelCatalogEntry,
    quantization: ModelQuantization,
    context_tokens: u32,
) -> u64 {
    let bits_per_weight = match quantization {
        ModelQuantization::Q3Km => 3.4,
        ModelQuantization::Q4Km => 4.5,
        ModelQuantization::Q5Km => 5.5,
        ModelQuantization::Q6K => 6.5,
        ModelQuantization::Q8 => 8.5,
    };
    // For installed models Ollama already reports the exact on-disk weight size.
    // Keep the selected quantization as a download choice for remote catalogs,
    // but do not pretend it changes a model which is already installed.
    let weights = if entry.source == ModelSource::Installed {
        entry.estimated_size.unwrap_or_else(|| {
            entry.parameter_count.map_or(0, |parameters| {
                (parameters as f64 * bits_per_weight / 8.0 * 1.08) as u64
            })
        })
    } else {
        entry.parameter_count.map_or_else(
            || entry.estimated_size.unwrap_or(0),
            |parameters| (parameters as f64 * bits_per_weight / 8.0 * 1.08) as u64,
        )
    };
    let parameter_billions = entry.parameter_count.unwrap_or(1_000_000_000) as f64 / 1e9;
    let context_reserve =
        (parameter_billions * f64::from(context_tokens) / 8_192.0 * 80.0 * 1_048_576.0) as u64;
    weights
        .saturating_add(context_reserve)
        .saturating_add(256 * 1_048_576)
}

#[cfg(test)]
fn hardware_recommendation(metrics: &RuntimeMetrics) -> String {
    let ram_gib = metrics.memory_total as f64 / 1_073_741_824.0;
    let vram_gib = metrics.vram_total as f64 / 1_073_741_824.0;
    if ram_gib < 12.0 {
        "FIT → qwen3.5:0.8b · Q4_K_M · 4K context".into()
    } else if ram_gib < 18.0 || vram_gib < 4.0 {
        "FIT → qwen3.5:2b · Q4_K_M · 8K context · one model resident".into()
    } else if ram_gib < 28.0 || vram_gib < 8.0 {
        "FIT → qwen3.5:4b · Q4_K_M · 16K context".into()
    } else {
        "FIT → 8B class · Q4_K_M · 16–32K context".into()
    }
}

fn truncate(value: &str, max: usize) -> String {
    if value.chars().count() <= max {
        return value.to_owned();
    }
    let mut result = value.chars().take(max).collect::<String>();
    result.pop();
    result.push('…');
    result
}

fn nonempty<'a>(value: &'a str, fallback: &'a str) -> &'a str {
    if value.trim().is_empty() {
        fallback
    } else {
        value
    }
}

fn centered(width_percent: u16, height: u16, area: Rect) -> Rect {
    let width = area.width.saturating_mul(width_percent) / 100;
    Rect::new(
        area.x + area.width.saturating_sub(width) / 2,
        area.y + area.height.saturating_sub(height.min(area.height)) / 2,
        width.max(20).min(area.width),
        height.min(area.height),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use omarag_app::{Action, update};
    use omarag_domain::{Citation, ModelProfilePreflight, PageEvidence, VisualEvidenceSelection};
    use ratatui::{Terminal, backend::TestBackend};

    fn rendered(width: u16, height: u16, state: &AppState, theme: Theme) -> String {
        rendered_metrics(width, height, state, theme, &RuntimeMetrics::default())
    }

    fn rendered_metrics(
        width: u16,
        height: u16,
        state: &AppState,
        theme: Theme,
        metrics: &RuntimeMetrics,
    ) -> String {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| render_with_metrics(frame, state, &theme, metrics))
            .unwrap();
        terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect()
    }

    fn rendered_runtime(
        width: u16,
        height: u16,
        state: &AppState,
        visual: &VisualInspectorState,
        hardware: &HardwareProfileResponse,
    ) -> String {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| {
                render_with_runtime(
                    frame,
                    state,
                    &Theme::default(),
                    &RuntimeMetrics::default(),
                    &mut [],
                    &mut [],
                    visual,
                    hardware,
                )
            })
            .unwrap();
        terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect()
    }

    fn visual_test_citation() -> Citation {
        Citation {
            evidence_id: Some("E1".into()),
            prompt_evidence_id: Some("E1".into()),
            chunk_id: "chunk-1".into(),
            chunk_ids: Vec::new(),
            document_id: Some("doc-1".into()),
            logical_document_id: None,
            source_uri: Some("/tmp/book.pdf".into()),
            document_title: Some("Book".into()),
            pages: vec![7],
            headings: Vec::new(),
            element_types: Vec::new(),
            doc_item_refs: Vec::new(),
            picture_refs: vec!["legacy-picture-ref".into()],
            primary_anchors: Vec::new(),
            context_anchors: Vec::new(),
            excerpt: "Evidence".into(),
            excerpt_char_start: None,
            excerpt_char_end: None,
            chunk_content_hash: None,
            retrieval_rank: None,
            rerank_score: None,
            claim_ids: Vec::new(),
            retrieval_paths: Vec::new(),
            relevance_score: None,
            book: None,
            verification_status: "verified".into(),
        }
    }

    fn visual_test_media(media_id: &str, kind: &str) -> MediaEvidence {
        MediaEvidence {
            media_id: media_id.into(),
            kind: kind.into(),
            bbox: Some(omarag_domain::MediaBoundingBox {
                x0: 0.1,
                y0: 0.1,
                x1: 0.8,
                y1: 0.8,
                coordinate_space: Some("normalized".into()),
            }),
            ..MediaEvidence::default()
        }
    }

    #[test]
    fn visual_inspector_keeps_cited_pages_and_media_assets_strictly_separate() {
        let mut state = AppState {
            view: View::Conversation,
            ..AppState::default()
        };
        state.chat.citations = vec![visual_test_citation()];
        let mut visual = VisualInspectorState::default();
        visual.replace(
            "run-1".into(),
            VisualEvidenceResponse {
                pages: vec![PageEvidence {
                    page_id: "page-7".into(),
                    citation_index: Some(0),
                    document_id: Some("doc-1".into()),
                    page: 7,
                    ..PageEvidence::default()
                }],
                media: vec![
                    visual_test_media("not-a-crop", "page_preview"),
                    visual_test_media("figure-1", "figure"),
                    visual_test_media("figure-2", "diagram"),
                    visual_test_media("figure-3", "table"),
                    visual_test_media("figure-4", "formula"),
                    visual_test_media("figure-5", "image"),
                ],
                selection: VisualEvidenceSelection {
                    max_media: 8,
                    cut_reason: None,
                },
                ..VisualEvidenceResponse::default()
            },
        );
        let content = rendered_runtime(
            160,
            48,
            &state,
            &visual,
            &HardwareProfileResponse::default(),
        );
        assert!(content.contains("Pages  1"));
        assert!(content.contains("Figures  4"));
        assert!(content.contains("Sources"));
        assert!(!content.contains("Page_preview"));
    }

    #[test]
    fn legacy_visual_fallback_has_pages_but_zero_fake_figures() {
        let mut state = AppState {
            view: View::Conversation,
            ..AppState::default()
        };
        state.chat.citations = vec![visual_test_citation()];
        let mut visual = VisualInspectorState::default();
        visual.use_legacy("run-old".into());
        let content = rendered_runtime(
            160,
            48,
            &state,
            &visual,
            &HardwareProfileResponse::default(),
        );
        assert!(content.contains("Pages  1"));
        assert!(content.contains("Figures"));
        assert!(
            content.contains("Figure extraction unavailable."),
            "{content}"
        );
    }

    #[test]
    fn compact_inspector_uses_pages_figures_and_sources_tabs() {
        let state = AppState {
            view: View::Conversation,
            focus_pane: FocusPane::Inspector,
            ..AppState::default()
        };
        let visual = VisualInspectorState {
            tab: VisualInspectorTab::Figures,
            ..VisualInspectorState::default()
        };
        let content = rendered_runtime(
            110,
            32,
            &state,
            &visual,
            &HardwareProfileResponse::default(),
        );
        assert!(content.contains("Pages"));
        assert!(content.contains("Figures"));
        assert!(content.contains("Sources"));
        assert!(content.contains("Figures"));
    }

    #[test]
    fn model_center_explains_server_tier_catalog_profile_and_expert_path() {
        let state = AppState {
            view: View::FoundryOverview,
            ..AppState::default()
        };
        let hardware = HardwareProfileResponse {
            tier: HardwareTier::new(7).unwrap(),
            limiting_factor: "VRAM".into(),
            catalog_version: "2026.08.1".into(),
            profile: PerformanceProfile::Normal,
            recommendations: vec![omarag_domain::ModelRecommendation {
                role: "chat".into(),
                model: "recommended-chat".into(),
                ..omarag_domain::ModelRecommendation::default()
            }],
            ..HardwareProfileResponse::default()
        };
        let content =
            rendered_runtime(160, 48, &state, &VisualInspectorState::default(), &hardware);
        assert!(content.contains("Tier 7 · Normal"));
        assert!(content.contains("VRAM"));
        assert!(content.contains("2026.08.1"));
        assert!(content.contains("Expert"));
        assert!(content.contains("Automatic stack"));
        assert!(content.contains("recommended-chat"));
    }

    #[test]
    fn automatic_stack_overlay_shows_changes_download_total_and_reindex_block() {
        let preflight: ModelProfilePreflight = serde_json::from_value(serde_json::json!({
            "recommendation": {
                "recommendation_id": "rec-1",
                "catalog_release": "2026.08",
                "profile": "normal",
                "stack_tier": 5,
                "assignments": [],
                "context_tokens": 8192,
                "total_download_bytes": 4_294_967_296_u64,
                "warnings": []
            },
            "changes": {"chat": "qwen/current:4b"},
            "downloads": [{
                "role": "chat",
                "artifact_id": "chat-1",
                "provider": "ollama",
                "model": "qwen/current:4b",
                "revision": "r1",
                "digest": "sha256:abc",
                "install_state": "not-installed",
                "download_bytes": 4_294_967_296_u64
            }],
            "requires_reindex": true,
            "requires_visual_reindex": false,
            "can_apply": false,
            "warnings": []
        }))
        .unwrap();
        let state = AppState {
            overlay: Some(Overlay::AutomaticStackPreflight),
            automatic_stack_preflight: Some(preflight),
            ..AppState::default()
        };

        let content = rendered(140, 40, &state, Theme::default());
        assert!(content.contains("Exact model changes"));
        assert!(content.contains("qwen/current:4b"));
        assert!(content.contains("4.0\u{a0}GiB"), "{content}");
        assert!(content.contains("YES — blocked here"));
        assert!(content.contains("Full rebuild required"));
    }

    fn rendered_rows_metrics(
        width: u16,
        height: u16,
        state: &AppState,
        theme: Theme,
        metrics: &RuntimeMetrics,
    ) -> Vec<String> {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| render_with_metrics(frame, state, &theme, metrics))
            .unwrap();
        let buffer = terminal.backend().buffer();
        (0..height)
            .map(|y| {
                (0..width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
            })
            .collect()
    }

    #[test]
    fn citation_label_prefers_prompt_id_over_stable_join_id() {
        assert_eq!(
            preferred_evidence_label(Some("E2"), Some("ev-stable-42")),
            "E2"
        );
        assert_eq!(
            preferred_evidence_label(None, Some("ev-stable-42")),
            "ev-stable-42"
        );
        assert_eq!(preferred_evidence_label(None, None), "E?");
    }

    #[test]
    fn wide_shell_contains_sidebar_workspace_and_inspector() {
        let content = rendered(160, 42, &AppState::default(), Theme::default());
        for title in [
            "Chat",
            "Library",
            "Models",
            "Settings",
            "Conversation",
            "Evidence",
            "cpu",
        ] {
            assert!(content.contains(title), "missing {title}");
        }
        assert!(!content.contains("Index new PDFs"));
    }

    #[test]
    fn sidebar_gives_cpu_ram_and_vram_their_own_rows() {
        let metrics = RuntimeMetrics {
            cpu_usage: 3.0,
            memory_used: 6 * 1_073_741_824,
            memory_total: 13 * 1_073_741_824,
            vram_used: 1_800_000_000,
            vram_total: 2_000_000_000,
            model_roles: vec![ModelRoleStatus {
                role: "chat".into(),
                model: Some("qwen3.5:4b-long-model-name".into()),
                residency: "idle".into(),
                shared_with: Vec::new(),
            }],
            ..RuntimeMetrics::default()
        };
        let rows = rendered_rows_metrics(160, 42, &AppState::default(), Theme::default(), &metrics);
        let cpu = rows.iter().position(|row| row.contains("cpu"));
        let ram = rows.iter().position(|row| row.contains("mem"));
        let vram = rows.iter().position(|row| row.contains("vram"));
        assert!(cpu.is_some() && ram.is_some() && vram.is_some());
        assert_ne!(cpu, ram);
        assert_ne!(ram, vram);
        assert_ne!(cpu, vram);
        // A role and the model filling it belong on one row.
        assert!(
            rows.iter()
                .any(|row| row.contains("chat") && row.contains("qwen3.5:4b")),
            "role and model should share a row"
        );
    }

    #[test]
    fn every_theme_is_named_uniquely_and_usable() {
        let themes = (0..Theme::count()).map(Theme::at).collect::<Vec<_>>();
        assert!(themes.len() >= 30, "expected the imported set");

        // Names must be unique — they are how a theme is selected and persisted.
        let names = themes
            .iter()
            .map(|theme| theme.name)
            .collect::<std::collections::BTreeSet<_>>();
        assert_eq!(names.len(), themes.len(), "duplicate theme name");

        // Backgrounds may repeat across variants of one family; what must hold
        // is that each theme is legible and its states are distinguishable.
        for theme in &themes {
            assert_ne!(
                theme.text, theme.background,
                "{}: invisible text",
                theme.name
            );
            assert_ne!(
                theme.selection, theme.background,
                "{}: invisible selection",
                theme.name
            );
            assert_ne!(
                theme.focus, theme.border,
                "{}: focus reads as idle",
                theme.name
            );
            assert_ne!(
                theme.workspace.border_active, theme.workspace.border,
                "{}: focused pane reads as unfocused",
                theme.name
            );
        }
    }

    #[test]
    fn focused_pane_has_a_high_contrast_accent() {
        let theme = Theme::default();
        let backend = TestBackend::new(120, 30);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| render(frame, &AppState::default(), &theme))
            .unwrap();
        assert!(
            terminal
                .backend()
                .buffer()
                .content()
                .iter()
                .any(|cell| cell.fg == theme.focus)
        );
    }

    #[test]
    fn header_shows_product_view_library_and_no_mythology() {
        let mut state = AppState {
            active_workspace: Some("library-1".into()),
            ..AppState::default()
        };
        state.workspaces.push(omarag_domain::WorkspaceSummary {
            id: "library-1".into(),
            name: "Concrete".into(),
            path: "/tmp/concrete".into(),
            read_only: false,
            updated_at: "2026-08-05T10:00:00Z".into(),
            etag: "etag".into(),
        });
        let idle = rendered_metrics(
            160,
            42,
            &state,
            Theme::default(),
            &RuntimeMetrics::default(),
        );
        assert!(idle.contains("OmaRag"));
        assert!(idle.contains("Conversation"));
        assert!(idle.contains("Concrete"));
        assert!(idle.contains("ready"));
        for retired in ["ORACLE", "METIS", "Metis", "ALETHEIA", "Aletheia", "◈≋◈"] {
            assert!(!idle.contains(retired), "header still shows {retired}");
        }
    }

    #[test]
    fn header_reports_real_index_progress_and_answer_phase() {
        let mut indexing = AppState {
            active_workspace: Some("library-1".into()),
            ..AppState::default()
        };
        indexing.jobs.insert(
            "job-1".into(),
            JobSnapshot {
                id: "job-1".into(),
                workspace_id: "library-1".into(),
                kind: "ingest".into(),
                status: JobStatus::Running,
                progress: 0.34,
                phase: "embedding".into(),
                payload: serde_json::json!({}),
                result: None,
                error: None,
                created_at: "2026-08-05T10:00:00Z".into(),
                updated_at: "2026-08-05T10:01:00Z".into(),
                last_event_id: None,
                checkpoint: None,
                progress_detail: None,
                pinned: false,
            },
        );
        let progress = rendered_metrics(
            160,
            42,
            &indexing,
            Theme::default(),
            &RuntimeMetrics {
                animation_tick: 2,
                ..RuntimeMetrics::default()
            },
        );
        assert!(progress.contains("indexing 34%"));

        indexing.jobs.get_mut("job-1").unwrap().progress_detail =
            Some(omarag_domain::JobProgressDetail {
                page_start: Some(26),
                page_end: Some(50),
                total_pages: Some(300),
                ..omarag_domain::JobProgressDetail::default()
            });
        indexing.chat.request_pending = true;
        indexing.chat.phase = "waiting".into();
        indexing.chat.phase_label = "Waiting".into();
        let waiting = rendered_metrics(
            160,
            42,
            &indexing,
            Theme::default(),
            &RuntimeMetrics {
                animation_tick: 3,
                ..RuntimeMetrics::default()
            },
        );
        assert!(waiting.contains("waiting"));
        assert!(!waiting.contains("indexing 34%"));
        assert!(waiting.contains("Waiting · indexing pages 26–50"));
    }

    #[test]
    fn omarchy_palette_maps_system_and_ansi_colors() {
        let theme = parse_omarchy_palette(
            r##"
                accent = "#ff8800"
                background = "#101112"
                foreground = "#e0e1e2"
                color1 = "#cc3344"
                color2 = "#55aa66"
                color3 = "#ddbb44"
                color5 = "#aa77dd"
                color6 = "#44bbcc"
                color9 = "#ff7744"
            "##,
            false,
        )
        .expect("valid Omarchy palette");

        assert_eq!(theme.name, "Omarchy System");
        assert_eq!(theme.background, rgb(0x101112));
        assert_eq!(theme.text, rgb(0xe0e1e2));
        assert_eq!(theme.focus, rgb(0xff8800));
        assert_eq!(theme.red, rgb(0xcc3344));
        assert_eq!(theme.orange, rgb(0xff7744));
        // The derived region roles must be usable, not collapsed onto the ground.
        assert_ne!(theme.panel, theme.background);
        assert_ne!(theme.selection, theme.background);
        assert_eq!(theme.workspace.border_active, rgb(0xff8800));
        assert_eq!(theme.mode, ThemeMode::Dark);
    }

    #[test]
    fn omarag_identity_and_both_companions_survive_minimum_width() {
        let compact = rendered_metrics(
            80,
            24,
            &AppState::default(),
            Theme::default(),
            &RuntimeMetrics::default(),
        );

        assert!(compact.contains("OmaRag"));
    }

    #[test]
    fn wide_geometry_uses_fixed_sidebar_and_inspector() {
        let areas = app_areas(Rect::new(0, 0, 160, 36), FocusPane::Workspace);
        assert_eq!(areas.sidebar.width, 24);
        assert_eq!(areas.inspector.width, 38);
        assert_eq!(areas.workspace.width, 96);
        assert_eq!(areas.workspace.x, 25);
        assert_eq!(areas.inspector.x, 122);
    }

    #[test]
    fn medium_geometry_swaps_workspace_for_inspector() {
        let body = Rect::new(0, 0, 110, 27);
        let workspace = app_areas(body, FocusPane::Workspace);
        assert_eq!(workspace.sidebar.width, 22);
        assert_eq!(workspace.workspace.width, 87);
        assert_eq!(workspace.inspector.width, 0);
        let inspector = app_areas(body, FocusPane::Inspector);
        assert_eq!(inspector.sidebar.width, 22);
        assert_eq!(inspector.workspace.width, 0);
        assert_eq!(inspector.inspector.width, 87);
    }

    #[test]
    fn narrow_geometry_shows_one_conceptual_pane() {
        let body = Rect::new(0, 0, 80, 21);
        let sidebar = app_areas(body, FocusPane::Sidebar);
        let workspace = app_areas(body, FocusPane::Workspace);
        let inspector = app_areas(body, FocusPane::Inspector);
        assert_eq!(sidebar.sidebar, body);
        assert_eq!(workspace.workspace, body);
        assert_eq!(inspector.inspector, body);
        assert_eq!(sidebar.workspace.width, 0);
        assert_eq!(workspace.inspector.width, 0);
    }

    #[test]
    fn undersized_terminal_renders_a_clear_message() {
        let content = rendered(79, 23, &AppState::default(), Theme::default());
        assert!(content.contains("needs 80×24"));
    }

    #[test]
    fn simple_mode_hides_advanced_context_views() {
        assert_eq!(
            section_views(PrimarySection::Library, InteractionLevel::Simple),
            vec![View::Books, View::Indexing]
        );
        assert!(
            section_views(PrimarySection::Library, InteractionLevel::Workshop)
                .contains(&View::Quality)
        );
    }

    #[test]
    fn sidebar_nests_children_directly_below_each_section() {
        assert_eq!(
            sidebar_navigation_rows(&AppState::default()),
            vec![
                Some(View::Conversation),
                Some(View::Conversation),
                Some(View::History),
                None,
                Some(View::Books),
                Some(View::Books),
                Some(View::Indexing),
                None,
                Some(View::FoundryOverview),
                Some(View::FoundryOverview),
                Some(View::Models),
                None,
                Some(View::Settings),
                Some(View::Settings),
                Some(View::Themes),
            ]
        );
        let advanced = AppState {
            interaction_level: InteractionLevel::Workshop,
            ..AppState::default()
        };
        assert_eq!(sidebar_navigation_rows(&advanced).len(), 20);
    }

    #[test]
    fn chat_geometry_stays_sane_under_extreme_content() {
        // A very long question, a very long answer, and the smallest supported
        // terminal: the composer must still exist and the answer must not be
        // given negative or overlapping space.
        let mut state = AppState {
            view: View::Conversation,
            ..AppState::default()
        };
        state.chat.submitted_question = "why ".repeat(400);
        state.chat.answer = "content ".repeat(2000);
        state.chat.question.set("a".repeat(500));

        for (width, height) in [(80, 24), (96, 30), (120, 34), (200, 60)] {
            let [_header, body, _footer] = screen_areas(Rect::new(0, 0, width, height));
            let inner = pane_inner(app_areas(body, FocusPane::Workspace).workspace);
            let areas = chat_areas(inner, &state);
            assert!(
                areas.composer.height >= 1,
                "composer vanished at {width}x{height}"
            );
            assert!(
                areas.composer.bottom() <= inner.bottom(),
                "composer overflows the pane at {width}x{height}"
            );
            assert!(
                areas.answer.y >= areas.question.bottom(),
                "answer overlaps the question at {width}x{height}"
            );
            assert!(
                areas.answer.bottom() <= areas.scope.y,
                "answer overlaps the composer label at {width}x{height}"
            );
            let _ = rendered_metrics(
                width,
                height,
                &state,
                Theme::default(),
                &RuntimeMetrics::default(),
            );
        }
    }

    #[test]
    fn every_view_and_overlay_renders_at_every_supported_size() {
        // Regression net for the redesign: each screen must still draw, at the
        // smallest supported terminal and at the responsive breakpoints, in both
        // interaction levels.
        const OVERLAYS: [Overlay; 18] = [
            Overlay::ConfirmQuit,
            Overlay::Help,
            Overlay::Palette,
            Overlay::Workspaces,
            Overlay::ConfirmModelDelete,
            Overlay::AutomaticStackPreflight,
            Overlay::AutomaticStackDownloadConfirm,
            Overlay::FileBrowser,
            Overlay::ConfirmImport,
            Overlay::DocumentDetails,
            Overlay::ConfirmDocumentDelete,
            Overlay::ConfirmLibraryDelete,
            Overlay::WorkspaceProfile,
            Overlay::CustomProfileEditor,
            Overlay::ChatHistory,
            Overlay::DocumentTags,
            Overlay::CustomModel,
            Overlay::BookScope,
        ];
        let sizes = [(80, 24), (96, 30), (120, 34), (200, 50)];

        for (width, height) in sizes {
            for level in [InteractionLevel::Simple, InteractionLevel::Workshop] {
                for view in View::ALL {
                    for pane in [
                        FocusPane::Sidebar,
                        FocusPane::Workspace,
                        FocusPane::Inspector,
                    ] {
                        let state = AppState {
                            view,
                            focus_pane: pane,
                            interaction_level: level,
                            ..AppState::default()
                        };
                        let _ = rendered_metrics(
                            width,
                            height,
                            &state,
                            Theme::default(),
                            &RuntimeMetrics::default(),
                        );
                    }
                }
                for overlay in OVERLAYS {
                    let state = AppState {
                        overlay: Some(overlay),
                        interaction_level: level,
                        ..AppState::default()
                    };
                    let _ = rendered_metrics(
                        width,
                        height,
                        &state,
                        Theme::default(),
                        &RuntimeMetrics::default(),
                    );
                }
            }
        }
    }

    #[test]
    fn every_theme_renders_the_shell() {
        for index in 0..Theme::count() {
            let content = rendered_metrics(
                120,
                34,
                &AppState::default(),
                Theme::at(index),
                &RuntimeMetrics::default(),
            );
            assert!(content.contains("OmaRag"), "theme {index} lost the header");
        }
    }

    #[test]
    fn sidebar_click_map_matches_the_rendered_rows() {
        // The mouse maps a row index to a view; if the two ever drift, clicks
        // land on the wrong entry.
        for level in [InteractionLevel::Simple, InteractionLevel::Workshop] {
            let state = AppState {
                interaction_level: level,
                ..AppState::default()
            };
            let rows = rendered_rows_metrics(
                160,
                42,
                &state,
                Theme::default(),
                &RuntimeMetrics::default(),
            );
            let map = sidebar_navigation_rows(&state);
            // Row 0 of the sidebar's content sits one row below the pane heading.
            let body = app_areas(screen_areas(Rect::new(0, 0, 160, 42))[1], state.focus_pane);
            let top = pane_inner(body.sidebar).y as usize;
            for (offset, entry) in map.iter().enumerate() {
                // Read inside the sidebar frame, past its border column.
                let inner = pane_inner(body.sidebar);
                let content_of = |row: usize| -> String {
                    rows[row]
                        .chars()
                        .skip(inner.x as usize)
                        .take(inner.width as usize)
                        .collect()
                };
                let Some(view) = entry else {
                    let column = content_of(top + offset);
                    assert!(column.trim().is_empty(), "row {offset} should be a spacer");
                    continue;
                };
                let column = content_of(top + offset);
                // A row is either the view itself, or the section heading that
                // opens that view as its default.
                assert!(
                    column.contains(view.label()) || column.contains(view.section().label()),
                    "row {offset} should reach {:?}, got {column:?}",
                    view.label()
                );
            }
        }
    }

    #[test]
    fn evidence_beyond_the_fourth_tile_stays_reachable() {
        // The grid holds four tiles. With more evidence than that, the page must
        // follow the selection instead of hiding the rest.
        assert_eq!(evidence_page_start(0), 0);
        assert_eq!(evidence_page_start(3), 0);
        assert_eq!(evidence_page_start(4), 4);
        assert_eq!(evidence_page_start(7), 4);
        assert_eq!(evidence_page_start(8), 8);

        // And the grid never hands back more tiles than asked for, however many
        // items exist.
        let area = Rect::new(0, 0, 40, 20);
        for visible in 0..=EVIDENCE_PAGE {
            assert_eq!(evidence_tiles(area, visible).len(), visible);
        }
    }

    #[test]
    fn each_pane_frame_wears_its_own_region_colour() {
        // The point of the region model: three stacked panels must not read as
        // one wall of accent. Catppuccin Mocha gives each region a different
        // active border upstream, so the shell must show three distinct colours.
        let theme =
            Theme::at(Theme::index_of("Catppuccin Mocha").expect("bundled Catppuccin Mocha"));
        assert_ne!(theme.sidebar.border_active, theme.workspace.border_active);
        assert_ne!(theme.footer.border_active, theme.workspace.border_active);

        let backend = TestBackend::new(160, 42);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| {
                render_with_metrics(
                    frame,
                    &AppState::default(),
                    &theme,
                    &RuntimeMetrics::default(),
                )
            })
            .unwrap();
        let buffer = terminal.backend().buffer();

        // The unfocused regions use their idle border; the focused one its
        // active border. Collect what the frame corners actually got painted.
        let corners: std::collections::HashSet<_> = buffer
            .content()
            .iter()
            .filter(|cell| matches!(cell.symbol(), "╭" | "╮" | "╰" | "╯"))
            .map(|cell| cell.fg)
            .collect();
        assert!(
            corners.len() >= 2,
            "frames all share one colour: {corners:?}"
        );
    }

    #[test]
    fn shell_frames_each_pane_with_a_rounded_titled_border() {
        let rows = rendered_rows_metrics(
            160,
            42,
            &AppState::default(),
            Theme::default(),
            &RuntimeMetrics::default(),
        );
        let content = rows.join("\n");
        // Three framed regions, each opening with a rounded corner.
        let frame_row = rows
            .iter()
            .find(|row| row.contains('╭'))
            .expect("a frame must be drawn");
        assert_eq!(
            frame_row.matches('╭').count(),
            3,
            "sidebar, workspace and inspector are each framed"
        );
        assert_eq!(frame_row.matches('╮').count(), 3);
        // Titles are cut into the top edge, superfile style.
        assert!(frame_row.contains("┤ Conversation ├"), "{frame_row}");
        // Rounded corners only: no square frame glyphs anywhere.
        for square in ['┌', '┐', '└', '┘'] {
            assert!(!content.contains(square), "square corner {square} found");
        }
        assert!(
            rows.iter()
                .any(|row| row.contains('╰') && row.contains('╯')),
            "frames must close"
        );
    }

    #[test]
    fn theme_action_cycles_at_runtime() {
        let mut state = AppState {
            theme_count: Theme::count(),
            ..AppState::default()
        };
        update(&mut state, Action::CycleTheme);
        assert_eq!(state.theme_index, 1);
        update(&mut state, Action::CycleTheme);
        assert_eq!(state.theme_index, 2);
        // And it wraps rather than running off the end.
        state.theme_index = Theme::count() - 1;
        update(&mut state, Action::CycleTheme);
        assert_eq!(state.theme_index, 0);
    }

    #[test]
    fn config_models_are_extracted() {
        let yaml = "qa:\n  model:\n    name: chat:4b\nembeddings:\n  model:\n    name: embed:1b\nreranking:\n  model: null\n";
        assert_eq!(config_model(yaml, "qa").as_deref(), Some("chat:4b"));
        assert_eq!(
            config_model(yaml, "embeddings").as_deref(),
            Some("embed:1b")
        );
        assert_eq!(config_model(yaml, "reranking"), None);
        let defaults = "oracle:\n  model_defaults:\n    chat: custom/chat\n    vl: custom/vision\n    embedding: custom/embed\n    rerank: custom/rank\n";
        assert_eq!(
            config_section_value(defaults, "model_defaults", "vl").as_deref(),
            Some("custom/vision")
        );
    }

    #[test]
    fn laptop_fit_prefers_two_billion_parameter_q4_model() {
        let metrics = RuntimeMetrics {
            memory_total: 14 * 1_073_741_824,
            memory_available: 7 * 1_073_741_824,
            vram_total: 2 * 1_073_741_824,
            ..RuntimeMetrics::default()
        };
        assert!(hardware_recommendation(&metrics).contains("qwen3.5:2b"));
        let entry = ModelCatalogEntry {
            parameter_count: Some(2_000_000_000),
            ..ModelCatalogEntry::default()
        };
        let q4 = estimated_model_memory(&entry, ModelQuantization::Q4Km, 8_192);
        let q8 = estimated_model_memory(&entry, ModelQuantization::Q8, 8_192);
        assert!(q4 < q8);
        assert!(q4 < 2 * 1_073_741_824);
    }

    #[test]
    fn installed_model_estimate_uses_reported_size_not_download_choice() {
        let entry = ModelCatalogEntry {
            source: ModelSource::Installed,
            parameter_count: Some(4_000_000_000),
            estimated_size: Some(2_500_000_000),
            ..ModelCatalogEntry::default()
        };
        let q3 = estimated_model_memory(&entry, ModelQuantization::Q3Km, 8_192);
        let q8 = estimated_model_memory(&entry, ModelQuantization::Q8, 8_192);
        assert_eq!(q3, q8);
    }

    #[test]
    fn integrated_models_render_presets_catalog_and_central_controls() {
        let mut state = AppState {
            view: View::FoundryOverview,
            ..AppState::default()
        };
        state.model_manager.entries.push(ModelCatalogEntry {
            id: "owner/tiny-GGUF".into(),
            source: ModelSource::HuggingFace,
            description: "A small multilingual model".into(),
            likes: Some(42),
            downloads: Some(12_000),
            parameter_count: Some(1_000_000_000),
            ..ModelCatalogEntry::default()
        });
        state.model_manager.packages.push(ModelPackage {
            id: "qwen-unified".into(),
            name: "Qwen Unified".into(),
            summary: "One model handles chat and images.".into(),
            synergy: "Qwen retrieval family".into(),
            recommended_rank: 1,
            total_estimated_memory: 2_000_000_000,
            fit: ModelFit::Comfortable,
            models: vec![omarag_app::ModelPackageItem {
                role: ModelCategory::Chat,
                model: "qwen3.5:2b".into(),
                download_name: "qwen3.5:2b".into(),
                source: ModelSource::Ollama,
                installed: false,
            }],
        });
        let setup = rendered(140, 40, &state, Theme::default());
        for expected in [
            "Roles",
            "Recommended for this device",
            "Qwen Unified",
            "Qwen retrieval family",
            "Setup",
            "Install package",
        ] {
            assert!(setup.contains(expected), "missing {expected}");
        }

        state.view = View::Models;
        let catalog = rendered(140, 40, &state, Theme::default());
        for expected in [
            "Catalog",
            "S Source",
            "owner/tiny-GGUF",
            "A small multilingual model",
            "Download",
            "Memory",
        ] {
            assert!(catalog.contains(expected), "missing {expected}");
        }
    }

    #[test]
    fn foundry_uses_the_shells_medium_and_narrow_pane_switching() {
        let mut state = AppState {
            view: View::FoundryOverview,
            ..AppState::default()
        };
        state.model_manager.packages.push(ModelPackage {
            name: "Balanced local".into(),
            summary: "A compact setup for this device.".into(),
            recommended_rank: 1,
            ..ModelPackage::default()
        });

        let medium_workspace = rendered(110, 30, &state, Theme::default());
        assert!(medium_workspace.contains("Recommended for this device"));
        assert!(!medium_workspace.contains("Stack details"));

        state.focus_pane = FocusPane::Inspector;
        let medium_inspector = rendered(110, 30, &state, Theme::default());
        assert!(medium_inspector.contains("Stack details"));
        assert!(!medium_inspector.contains("Setup"));

        state.focus_pane = FocusPane::Workspace;
        let narrow_workspace = rendered(80, 24, &state, Theme::default());
        assert!(narrow_workspace.contains("Roles"));
        state.focus_pane = FocusPane::Inspector;
        let narrow_inspector = rendered(80, 24, &state, Theme::default());
        assert!(narrow_inspector.contains("Stack details"));
        assert!(!narrow_inspector.contains("Setup"));
    }

    #[test]
    fn markdown_is_rendered_and_unsafe_terminal_markup_is_removed() {
        let theme = Theme::default();
        let text = highlighted_answer(
            "## Result\n\n**bold** and *italic* with `code` [1].\n\n<script>bad</script>\u{1b}[31m",
            0,
            &theme,
        );
        let rendered = text
            .lines
            .iter()
            .flat_map(|line| line.spans.iter())
            .map(|span| span.content.as_ref())
            .collect::<String>();
        assert!(rendered.contains("Result"));
        assert!(rendered.contains("bold"));
        assert!(rendered.contains("italic"));
        assert!(rendered.contains("code"));
        assert!(rendered.contains("[1]"));
        assert!(!rendered.contains("**"));
        assert!(!rendered.contains("<script>"));
        assert!(!rendered.contains('\u{1b}'));
        assert!(!rendered.contains("[31m"));

        let bold = text
            .lines
            .iter()
            .flat_map(|line| line.spans.iter())
            .find(|span| span.content == "bold")
            .expect("bold span");
        assert!(bold.style.add_modifier.contains(Modifier::BOLD));
    }

    #[test]
    fn scientific_markdown_uses_terminal_safe_math_and_hides_figure_cross_references() {
        let text = highlighted_answer(
            "Mit \\(d_1\\), \\(d_2\\), \\(\\rho\\) und \\(N/mm^2\\) (Abb. 78). [E1]\n\nAbb. 78: Ausbreitmaßklassen",
            0,
            &Theme::default(),
        );
        let rendered = text
            .lines
            .iter()
            .flat_map(|line| line.spans.iter())
            .map(|span| span.content.as_ref())
            .collect::<String>();

        assert!(rendered.contains("d₁"));
        assert!(rendered.contains("d₂"));
        assert!(rendered.contains('ρ'));
        assert!(rendered.contains("N/mm²"));
        assert!(rendered.contains("[E1]"));
        assert!(!rendered.contains("Abb."));
        assert!(!rendered.contains("d_1"));
    }

    #[test]
    fn markdown_table_headers_are_visually_distinct() {
        let theme = Theme::default();
        let text = highlighted_answer(
            "| Klasse | Ausbreitmaß |\n|---|---|\n| F1 | ≤ 340 mm |",
            0,
            &theme,
        );
        let header = text
            .lines
            .iter()
            .flat_map(|line| line.spans.iter())
            .find(|span| span.content.contains("Klasse"))
            .expect("table header");

        assert!(header.style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(header.style.fg, Some(theme.focus));
    }

    #[test]
    fn rendered_answer_can_be_selected_and_only_markdown_bold_terms_are_clickable() {
        let answer = "plain **Beton** and Beton";
        assert_eq!(chat_answer_offset(answer, 0, 80, 0, 7, 0), Some(7));
        assert_eq!(
            chat_selection_text(
                answer,
                0,
                80,
                ChatTextSelection {
                    anchor: 6,
                    focus: 10,
                    moved: true,
                },
            )
            .as_deref(),
            Some("Beton")
        );
        assert_eq!(
            chat_bold_term_at(answer, 0, 80, 0, 7, 0).as_deref(),
            Some("Beton")
        );
        assert_eq!(chat_bold_term_at(answer, 0, 80, 0, 18, 0), None);
    }
}
