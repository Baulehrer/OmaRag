pub mod input;

use input::{filtered_palette_commands, fuzzy_score};
use omarag_app::{
    AppState, ChatTextSelection, ConnectionState, EditorState, FocusPane, InputMode,
    InteractionLevel, LibraryFilter, LibrarySort, ModelCatalogEntry, ModelFit, ModelPackage,
    ModelSource, Overlay, PrimarySection, THEME_COUNT, View, WorkspaceProfile,
};
#[cfg(test)]
use omarag_app::{ModelCategory, ModelQuantization};
use omarag_domain::{AnswerCacheStatus, JobSnapshot, JobStatus, SourceCheck};
use pulldown_cmark::{
    Event as MarkdownEvent, HeadingLevel, Options as MarkdownOptions, Parser, Tag, TagEnd,
};
use ratatui::{
    Frame,
    layout::{Alignment, Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, BorderType, Borders, Clear, List, ListItem, ListState, Paragraph, Wrap},
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
use unicode_width::UnicodeWidthChar;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Theme {
    pub name: &'static str,
    pub background: Color,
    pub surface: Color,
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
}

impl Theme {
    pub const COUNT: usize = THEME_COUNT;

    pub fn at(index: usize) -> Self {
        match index % Self::COUNT {
            0 => Self {
                name: "Aqua Slate",
                background: rgb(0x0D1117),
                surface: rgb(0x111820),
                panel: rgb(0x161D25),
                text: rgb(0xDCE7EA),
                muted: rgb(0x81909B),
                border: rgb(0x34434D),
                focus: rgb(0x5FD7D7),
                cyan: rgb(0x67D4E8),
                green: rgb(0x8FD19E),
                yellow: rgb(0xE7C66A),
                red: rgb(0xEE7B86),
                purple: rgb(0xB8A1E3),
                orange: rgb(0xE9A66B),
                selection: rgb(0x1D333A),
            },
            1 => Self {
                name: "One Dark",
                background: rgb(0x1E2127),
                surface: rgb(0x242831),
                panel: rgb(0x282C34),
                text: rgb(0xABB2BF),
                muted: rgb(0x7F848E),
                border: rgb(0x3E4451),
                focus: rgb(0x61AFEF),
                cyan: rgb(0x56B6C2),
                green: rgb(0x98C379),
                yellow: rgb(0xE5C07B),
                red: rgb(0xE06C75),
                purple: rgb(0xC678DD),
                orange: rgb(0xD19A66),
                selection: rgb(0x2C4057),
            },
            2 => Self {
                name: "Catppuccin Mocha",
                background: rgb(0x11111B),
                surface: rgb(0x181825),
                panel: rgb(0x1E1E2E),
                text: rgb(0xCDD6F4),
                muted: rgb(0xA6ADC8),
                border: rgb(0x45475A),
                focus: rgb(0x89DCEB),
                cyan: rgb(0x89DCEB),
                green: rgb(0xA6E3A1),
                yellow: rgb(0xF9E2AF),
                red: rgb(0xF38BA8),
                purple: rgb(0xCBA6F7),
                orange: rgb(0xFAB387),
                selection: rgb(0x313244),
            },
            3 => Self {
                name: "Solarized Lite",
                background: rgb(0xFDF6E3),
                surface: rgb(0xEEE8D5),
                panel: rgb(0xF5EEDC),
                text: rgb(0x586E75),
                muted: rgb(0x839496),
                border: rgb(0xC9C2AD),
                focus: rgb(0x268BD2),
                cyan: rgb(0x2AA198),
                green: rgb(0x859900),
                yellow: rgb(0xB58900),
                red: rgb(0xDC322F),
                purple: rgb(0x6C71C4),
                orange: rgb(0xCB4B16),
                selection: rgb(0xDDE7E5),
            },
            4 => dark_theme(
                "Metis Forge",
                0x100F0D,
                0x171512,
                0x201C17,
                0xE8DED0,
                0x918579,
                0x4A3B2F,
                0xE09F52,
                0x35271B,
            ),
            5 => dark_theme(
                "Aegean Night",
                0x08131F,
                0x0C1B2B,
                0x10243A,
                0xD8E8F2,
                0x7892A5,
                0x28465D,
                0x4DD4C6,
                0x153B49,
            ),
            6 => dark_theme(
                "Blueprint",
                0x071A2D,
                0x0B2640,
                0x0E3151,
                0xE7F3FF,
                0x83A7C4,
                0x2C5D7F,
                0x58B9FF,
                0x15476A,
            ),
            7 => dark_theme(
                "Ember Terminal",
                0x0C0A08,
                0x15110C,
                0x1E170F,
                0xF0E4CE,
                0x998976,
                0x4B3923,
                0xFFB000,
                0x3A2913,
            ),
            8 => dark_theme(
                "Neon Vector",
                0x0B0820,
                0x12102B,
                0x1A1539,
                0xEDE9FF,
                0x8D86B3,
                0x40366B,
                0x00E5FF,
                0x282053,
            ),
            9 => dark_theme(
                "Forest Circuit",
                0x08140F,
                0x0D1D16,
                0x12271D,
                0xDDE9E1,
                0x799183,
                0x2D4A39,
                0x68D391,
                0x193B29,
            ),
            10 => dark_theme(
                "Lunar Violet",
                0x101018,
                0x171722,
                0x20202E,
                0xE6E2F0,
                0x8E899D,
                0x3E3A50,
                0xA78BFA,
                0x302A47,
            ),
            11 => light_theme(
                "Arctic Paper",
                0xF4F8FB,
                0xE9F0F5,
                0xF8FBFD,
                0x24313A,
                0x6D7F8B,
                0xB8C8D2,
                0x007C91,
                0xD4EBF0,
            ),
            12 => light_theme(
                "Drafting Table",
                0xF5F1E8,
                0xE9E2D5,
                0xFBF8F1,
                0x2E2B27,
                0x756E64,
                0xC3B8A7,
                0x246B78,
                0xD9E8E5,
            ),
            13 => light_theme(
                "Bauhaus Signal",
                0xF6F3EA,
                0xEAE5D9,
                0xFFFDF7,
                0x1D1D1B,
                0x6F6C64,
                0xB8B1A3,
                0x0057B8,
                0xDCE7F5,
            ),
            _ => omarchy_theme_cache()
                .read()
                .ok()
                .and_then(|theme| *theme)
                .unwrap_or_else(omarchy_fallback),
        }
    }

    /// Refresh the palette used by the automatic Omarchy theme.
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

const fn rgb(hex: u32) -> Color {
    Color::Rgb(
        ((hex >> 16) & 0xff) as u8,
        ((hex >> 8) & 0xff) as u8,
        (hex & 0xff) as u8,
    )
}

fn omarchy_theme_cache() -> &'static RwLock<Option<Theme>> {
    static THEME: OnceLock<RwLock<Option<Theme>>> = OnceLock::new();
    THEME.get_or_init(|| RwLock::new(None))
}

fn omarchy_fallback() -> Theme {
    dark_theme(
        "Omarchy System",
        0x101218,
        0x171A22,
        0x1D222C,
        0xE7EAF0,
        0x939BAA,
        0x3D4655,
        0xD08770,
        0x352A2A,
    )
}

fn omarchy_theme_path() -> Option<PathBuf> {
    let config_home = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))?;
    Some(config_home.join("omarchy").join("current").join("theme"))
}

fn load_omarchy_theme() -> Option<Theme> {
    let theme_dir = omarchy_theme_path()?;
    let palette = fs::read_to_string(theme_dir.join("colors.toml")).ok()?;
    parse_omarchy_palette(&palette, theme_dir.join("light.mode").is_file())
}

fn parse_omarchy_palette(palette: &str, light: bool) -> Option<Theme> {
    let color = |key: &str| {
        palette.lines().find_map(|line| {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                return None;
            }
            let (candidate, value) = line.split_once('=')?;
            (candidate.trim() == key)
                .then(|| parse_hex_color(value.split(" #").next().unwrap_or(value).trim()))
                .flatten()
        })
    };
    let background = color("background")?;
    let text = color("foreground")?;
    let focus = color("accent").or_else(|| color("color4"))?;
    let surface = mix_color(background, text, if light { 6 } else { 5 });
    let panel = mix_color(background, text, if light { 3 } else { 9 });
    let border = mix_color(background, text, 25);
    let muted = mix_color(text, background, 38);
    let selection = mix_color(background, focus, if light { 18 } else { 28 });
    Some(Theme {
        name: "Omarchy System",
        background,
        surface,
        panel,
        text,
        muted,
        border,
        focus,
        cyan: color("color6").unwrap_or(focus),
        green: color("color2").unwrap_or(focus),
        yellow: color("color3").unwrap_or(focus),
        red: color("color1").unwrap_or(focus),
        purple: color("color5").unwrap_or(focus),
        orange: color("color9").or_else(|| color("color3")).unwrap_or(focus),
        selection,
    })
}

fn parse_hex_color(value: &str) -> Option<Color> {
    let value = value.trim_matches(|character| character == '"' || character == '\'');
    let value = value
        .strip_prefix('#')
        .or_else(|| value.strip_prefix("0x"))?;
    if value.len() != 6 {
        return None;
    }
    let hex = u32::from_str_radix(value, 16).ok()?;
    Some(rgb(hex))
}

fn mix_color(left: Color, right: Color, right_percent: u16) -> Color {
    let (Color::Rgb(lr, lg, lb), Color::Rgb(rr, rg, rb)) = (left, right) else {
        return left;
    };
    let mix = |left: u8, right: u8| {
        ((u16::from(left) * (100 - right_percent) + u16::from(right) * right_percent) / 100) as u8
    };
    Color::Rgb(mix(lr, rr), mix(lg, rg), mix(lb, rb))
}

#[allow(clippy::too_many_arguments)]
const fn dark_theme(
    name: &'static str,
    background: u32,
    surface: u32,
    panel: u32,
    text: u32,
    muted: u32,
    border: u32,
    focus: u32,
    selection: u32,
) -> Theme {
    Theme {
        name,
        background: rgb(background),
        surface: rgb(surface),
        panel: rgb(panel),
        text: rgb(text),
        muted: rgb(muted),
        border: rgb(border),
        focus: rgb(focus),
        cyan: rgb(0x67D4E8),
        green: rgb(0x8FD19E),
        yellow: rgb(0xE7C66A),
        red: rgb(0xEE7B86),
        purple: rgb(0xB8A1E3),
        orange: rgb(0xE9A66B),
        selection: rgb(selection),
    }
}

#[allow(clippy::too_many_arguments)]
const fn light_theme(
    name: &'static str,
    background: u32,
    surface: u32,
    panel: u32,
    text: u32,
    muted: u32,
    border: u32,
    focus: u32,
    selection: u32,
) -> Theme {
    Theme {
        name,
        background: rgb(background),
        surface: rgb(surface),
        panel: rgb(panel),
        text: rgb(text),
        muted: rgb(muted),
        border: rgb(border),
        focus: rgb(focus),
        cyan: rgb(0x007F91),
        green: rgb(0x3D7F4A),
        yellow: rgb(0x936B00),
        red: rgb(0xB63E4D),
        purple: rgb(0x6E50A0),
        orange: rgb(0xB85D16),
        selection: rgb(selection),
    }
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
    frame.render_widget(
        Block::default().style(Style::default().bg(theme.background).fg(theme.text)),
        frame.area(),
    );
    if frame.area().width < 80 || frame.area().height < 24 {
        render_minimum_size(frame, frame.area(), theme);
        return;
    }
    let [header, body, footer] = screen_areas(frame.area());
    render_header(frame, header, state, theme, metrics);
    let areas = app_areas(body, state.focus_pane);
    if areas.sidebar.width > 0 {
        render_sidebar(frame, areas.sidebar, state, theme, metrics);
    }
    if areas.workspace.width > 0 {
        render_workspace(frame, areas.workspace, state, theme, metrics, previews);
    }
    if areas.inspector.width > 0 {
        render_inspector(frame, areas.inspector, state, theme, metrics, previews);
    }
    render_footer(frame, footer, state, theme);
    render_overlay(frame, state, theme);
}

fn render_header(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    let ollama_active = state.chat.request_pending
        || state.chat.active_run.is_some()
        || state.model_manager.busy
        || state.jobs.values().any(|job| !is_terminal(&job.status));
    let pulse_phase = (metrics.animation_tick / 3) % 4;
    let pulse_color = if matches!(&state.connection, ConnectionState::Disconnected { .. }) {
        theme.red
    } else if ollama_active {
        match pulse_phase {
            0 => theme.orange,
            1 => theme.purple,
            2 => theme.cyan,
            _ => theme.green,
        }
    } else {
        theme.green
    };
    let block = Block::default()
        .borders(Borders::BOTTOM)
        .border_style(Style::default().fg(pulse_color))
        .style(Style::default().bg(theme.panel).fg(theme.text));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let [identity, companions] =
        Layout::horizontal([Constraint::Length(41), Constraint::Fill(1)]).areas(inner);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(" ◈", Style::default().fg(theme.purple)),
            Span::styled("≋", Style::default().fg(theme.orange)),
            Span::styled("◈", Style::default().fg(theme.cyan)),
            Span::styled(
                "  OmaRag",
                Style::default()
                    .fg(pulse_color)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(" · ", Style::default().fg(theme.muted)),
            Span::styled("ORACLE", Style::default().fg(theme.orange)),
            Span::styled(" OF ", Style::default().fg(theme.muted)),
            Span::styled("METIS", Style::default().fg(theme.purple)),
            Span::styled(" & ", Style::default().fg(theme.muted)),
            Span::styled("ALETHEIA", Style::default().fg(theme.cyan)),
        ])),
        identity,
    );
    let (metis, aletheia) = companion_poses(metrics.animation_tick);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled("Metis ", Style::default().fg(theme.purple)),
            Span::styled(metis, Style::default().fg(theme.purple)),
            Span::raw("   "),
            Span::styled("Aletheia ", Style::default().fg(theme.cyan)),
            Span::styled(aletheia, Style::default().fg(theme.cyan)),
        ]))
        .alignment(Alignment::Right),
        companions,
    );
}

fn companion_poses(tick: u64) -> (&'static str, &'static str) {
    const METIS: [&str; 4] = ["╱◆╲", "─◆╲", "╱◆─", "╱◇╲"];
    const ALETHEIA: [&str; 4] = ["╱◇╲", "╱◇─", "─◇╲", "╱◆╲"];
    let frame = ((tick / 2) % 4) as usize;
    (METIS[frame], ALETHEIA[frame])
}

pub(crate) fn screen_areas(area: Rect) -> [Rect; 3] {
    Layout::vertical([
        Constraint::Length(2),
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

pub(crate) fn app_areas(area: Rect, focus: FocusPane) -> AppAreas {
    if area.width >= 120 {
        let [sidebar, _, workspace, _, inspector] = Layout::horizontal([
            Constraint::Length(24),
            Constraint::Length(1),
            Constraint::Fill(1),
            Constraint::Length(1),
            Constraint::Length(38),
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
            Line::styled(
                "◈≋◈ OmaRag",
                Style::default()
                    .fg(theme.focus)
                    .add_modifier(Modifier::BOLD),
            ),
            Line::from(""),
            Line::styled(
                "The forge needs a little more room.",
                Style::default().fg(theme.text),
            ),
            Line::styled(
                format!("Current: {}×{}  ·  Minimum: 80×24", area.width, area.height),
                Style::default().fg(theme.muted),
            ),
            Line::from(""),
            Line::styled(
                "Resize the terminal to continue.",
                Style::default().fg(theme.yellow),
            ),
        ])
        .alignment(Alignment::Center)
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(theme.focus))
                .style(Style::default().bg(theme.surface)),
        ),
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

fn sidebar_child_line(label: &str, active: bool, focused: bool, theme: &Theme) -> Line<'static> {
    let style = if active {
        Style::default()
            .fg(theme.focus)
            .bg(theme.selection)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(theme.text)
    };
    Line::from(vec![
        Span::raw("  "),
        Span::styled(
            if active { "│" } else { " " },
            Style::default().fg(theme.focus),
        ),
        Span::styled(if active && focused { "› " } else { "  " }, style),
        Span::styled(label.to_owned(), style),
    ])
}

fn sidebar_heading(label: &str, active: bool, theme: &Theme) -> Line<'static> {
    Line::styled(
        format!(" {label}"),
        Style::default()
            .fg(if active { theme.focus } else { theme.muted })
            .add_modifier(Modifier::BOLD),
    )
}

pub(crate) fn sidebar_navigation_rows(state: &AppState) -> Vec<Option<View>> {
    let mut rows = Vec::new();
    for section in PrimarySection::CORE.iter().copied() {
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

fn render_sidebar(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    let workspace = state
        .active_workspace
        .as_ref()
        .and_then(|id| state.workspaces.iter().find(|item| &item.id == id))
        .map_or("No library", |item| item.name.as_str());
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(
            Style::default().fg(if state.focus_pane == FocusPane::Sidebar {
                theme.focus
            } else {
                theme.border
            }),
        )
        .title(Line::from(vec![
            Span::styled(" ◇ ", Style::default().fg(theme.cyan)),
            Span::styled(
                truncate(workspace, area.width.saturating_sub(7) as usize),
                Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
            ),
            Span::raw(" "),
        ]))
        .style(Style::default().bg(theme.surface).fg(theme.text));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let instrument_height = 13.min(inner.height);
    let [navigation, instruments] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(instrument_height)]).areas(inner);

    let section = state.view.section();
    let mut lines = Vec::new();
    for item in PrimarySection::CORE.iter().copied() {
        lines.push(sidebar_heading(
            &item.label().to_ascii_uppercase(),
            item == section,
            theme,
        ));
        for view in section_views(item, state.interaction_level) {
            lines.push(sidebar_child_line(
                view.label(),
                state.view == view,
                state.focus_pane == FocusPane::Sidebar,
                theme,
            ));
        }
    }
    let active_row = sidebar_navigation_rows(state)
        .iter()
        .rposition(|row| *row == Some(state.view))
        .unwrap_or_default() as u16;
    let navigation_scroll = active_row.saturating_sub(navigation.height.saturating_sub(1));
    frame.render_widget(
        Paragraph::new(lines).scroll((navigation_scroll, 0)),
        navigation,
    );

    let role_lines = if metrics.model_roles.is_empty() {
        configured_models(state)
            .into_iter()
            .flat_map(|(role, model)| {
                let loaded = metrics
                    .loaded_models
                    .iter()
                    .any(|item| model_matches(&item.name, &model));
                model_role_lines(
                    &role,
                    (model != "not configured").then_some(model.as_str()),
                    if loaded { "loaded" } else { "idle" },
                    area.width,
                    theme,
                )
            })
            .collect::<Vec<_>>()
    } else {
        metrics
            .model_roles
            .iter()
            .flat_map(|role| {
                model_role_lines(
                    &role.role,
                    role.model.as_deref(),
                    &role.residency,
                    area.width,
                    theme,
                )
            })
            .collect::<Vec<_>>()
    };
    let vram = if metrics.vram_total > 0 {
        format!(
            "VRAM   {} / {}",
            compact_memory(metrics.vram_used),
            compact_memory(metrics.vram_total)
        )
    } else {
        "VRAM   system shared".into()
    };
    let mut instrument_lines = vec![
        Line::styled(
            " INSTRUMENTS",
            Style::default()
                .fg(theme.muted)
                .add_modifier(Modifier::BOLD),
        ),
        Line::styled(
            format!(" CPU    {:>3.0}%", metrics.cpu_usage),
            Style::default().fg(theme.green),
        ),
        Line::styled(
            format!(
                " RAM    {} / {}",
                compact_memory(metrics.memory_used),
                compact_memory(metrics.memory_total)
            ),
            Style::default().fg(theme.yellow),
        ),
        Line::styled(format!(" {vram}"), Style::default().fg(theme.purple)),
    ];
    instrument_lines.extend(role_lines);
    frame.render_widget(
        Paragraph::new(instrument_lines).block(
            Block::default()
                .borders(Borders::TOP)
                .border_style(Style::default().fg(theme.border)),
        ),
        instruments,
    );
}

fn workspace_block(title: &str, focused: bool, theme: &Theme) -> Block<'static> {
    Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(if focused { theme.focus } else { theme.border }))
        .title(Line::from(vec![
            Span::styled(" ─ ", Style::default().fg(theme.focus)),
            Span::styled(
                title.to_owned(),
                Style::default()
                    .fg(theme.focus)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(" ", Style::default()),
        ]))
        .style(Style::default().bg(theme.background).fg(theme.text))
}

fn render_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    previews: &mut [ChatImagePreview],
) {
    let block = workspace_block(
        state.view.label(),
        state.focus_pane == FocusPane::Workspace,
        theme,
    );
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.width < 20 || inner.height < 4 {
        return;
    }
    match state.view {
        View::Conversation => render_chat_workspace(frame, inner, state, theme, metrics, previews),
        View::History => render_history_workspace(frame, inner, state, theme),
        View::Retrieval => render_retrieval_workspace(frame, inner, state, theme),
        View::Books => render_books_workspace(frame, inner, state, theme),
        View::Indexing => render_indexing_workspace(frame, inner, state, theme, metrics),
        View::Sources => render_sources_workspace(frame, inner, state, theme),
        View::Quality => render_quality_workspace(frame, inner, state, theme),
        View::Backups => render_backups_workspace(frame, inner, state, theme),
        View::FoundryOverview => render_foundry_workspace(frame, inner, state, theme, metrics),
        View::Models => render_models_workspace(frame, inner, state, theme, metrics),
        View::System => render_system_workspace(frame, inner, state, theme, metrics),
        View::Activity => render_activity_workspace(frame, inner, state, theme, metrics),
        View::Settings => render_settings_workspace(frame, inner, state, theme),
        View::Themes => render_themes_workspace(frame, inner, state, theme),
    }
}

fn render_chat_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    _previews: &mut [ChatImagePreview],
) {
    let input_height = if area.height >= 9 { 3 } else { 1 };
    let [body, input] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(input_height)]).areas(area);
    let (text, already_wrapped) = if let Some(error) = &state.chat.error {
        (
            Text::from(vec![
                Line::styled("Answer failed", Style::default().fg(theme.red)),
                Line::from(error.clone()),
            ]),
            false,
        )
    } else if state.chat.answer.is_empty() {
        (
            Text::from(vec![
                Line::from(""),
                Line::styled(
                    if state.chat.request_pending || state.chat.active_run.is_some() {
                        format!(
                            "{} Searching your library…",
                            spinner(metrics.animation_tick)
                        )
                    } else {
                        "Ask a question across your private library.".into()
                    },
                    Style::default()
                        .fg(theme.focus)
                        .add_modifier(Modifier::BOLD),
                ),
                Line::from(""),
                Line::styled(
                    "Answers stay local and carry their evidence into the inspector.",
                    Style::default().fg(theme.muted),
                ),
            ]),
            false,
        )
    } else {
        (
            selectable_answer(
                &state.chat.answer,
                state.citation_cursor,
                theme,
                body.width,
                state.chat.selection,
            ),
            true,
        )
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
    render_inline_editor(
        frame,
        input,
        &state.chat.question,
        "Ask your library · Enter send · Ctrl+E evidence",
        state.focus_pane == FocusPane::Workspace && state.input_mode == InputMode::Text,
        theme,
    );
}

fn render_books_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let [summary, list, actions] = Layout::vertical([
        Constraint::Length(2),
        Constraint::Fill(1),
        Constraint::Length(3),
    ])
    .areas(area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(
                    format!("{} books", state.documents.len()),
                    Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    format!(
                        "  ·  {}  ·  {}",
                        state.library.filter.label(),
                        state.library.sort.label()
                    ),
                    Style::default().fg(theme.muted),
                ),
            ]),
            Line::styled(
                "/ search  ·  I add PDFs  ·  F filter  ·  O sort",
                Style::default().fg(theme.muted),
            ),
        ]),
        summary,
    );
    let documents = library_documents(state);
    let items = if documents.is_empty() {
        vec![ListItem::new(vec![
            Line::styled("No indexed books yet", Style::default().fg(theme.muted)),
            Line::styled(
                "Press I to add a PDF or folder.",
                Style::default().fg(theme.focus),
            ),
        ])]
    } else {
        documents
            .iter()
            .map(|document| {
                let detail = state.library.details.get(&document.id);
                let pages = document
                    .page_count
                    .map_or("?".into(), |value| value.to_string());
                ListItem::new(vec![
                    Line::from(vec![
                        Span::styled(" PDF  ", Style::default().fg(theme.cyan)),
                        Span::styled(
                            &document.title,
                            Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
                        ),
                    ]),
                    Line::styled(
                        format!(
                            "      {pages} pages · {} · {}",
                            document.status,
                            detail.map_or("size unknown".into(), |item| format_bytes(
                                item.size_bytes
                            ))
                        ),
                        Style::default().fg(theme.muted),
                    ),
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
    frame.render_stateful_widget(
        List::new(items).highlight_symbol("│› ").highlight_style(
            Style::default()
                .bg(theme.selection)
                .fg(theme.focus)
                .add_modifier(Modifier::BOLD),
        ),
        list,
        &mut list_state,
    );
    frame.render_widget(
        Paragraph::new(vec![
            shortcut_words(
                theme,
                &[
                    ("Open", 'O', theme.green),
                    ("Info", 'N', theme.cyan),
                    ("Tags", 'T', theme.yellow),
                    ("Delete", 'D', theme.red),
                ],
            ),
            Line::styled(
                "Enter opens the selected book",
                Style::default().fg(theme.muted),
            ),
        ]),
        actions,
    );
}

fn render_indexing_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    let [intro, jobs, actions] = Layout::vertical([
        Constraint::Length(3),
        Constraint::Fill(1),
        Constraint::Length(2),
    ])
    .areas(area);
    let active = state
        .jobs
        .values()
        .filter(|job| !is_terminal(&job.status))
        .count();
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(
                    format!("{} active", active),
                    Style::default()
                        .fg(if active > 0 {
                            theme.yellow
                        } else {
                            theme.green
                        })
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    format!("  ·  {} total jobs", state.jobs.len()),
                    Style::default().fg(theme.muted),
                ),
            ]),
            Line::styled(
                "Import is a pipeline: discover → parse → chunk → embed → verify",
                Style::default().fg(theme.muted),
            ),
            Line::styled("I add PDFs or folders", Style::default().fg(theme.focus)),
        ]),
        intro,
    );
    let items = if state.jobs.is_empty() {
        vec![ListItem::new(Line::styled(
            "No indexing runs",
            Style::default().fg(theme.muted),
        ))]
    } else {
        state
            .jobs
            .values()
            .map(|job| activity_item(job, theme))
            .collect()
    };
    let mut list_state = ListState::default();
    list_state.select(
        (!state.jobs.is_empty())
            .then_some(state.job_cursor.min(state.jobs.len().saturating_sub(1))),
    );
    frame.render_stateful_widget(
        List::new(items)
            .highlight_symbol("│› ")
            .highlight_style(Style::default().bg(theme.selection)),
        jobs,
        &mut list_state,
    );
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                format!("{} ", spinner(metrics.animation_tick)),
                Style::default().fg(if active > 0 {
                    theme.yellow
                } else {
                    theme.muted
                }),
            ),
            Span::styled(
                "Space pause/resume  ·  X cancel  ·  R refresh",
                Style::default().fg(theme.muted),
            ),
        ])),
        actions,
    );
}

fn render_history_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
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
                        Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
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
    frame.render_stateful_widget(
        List::new(items)
            .highlight_symbol("│› ")
            .highlight_style(Style::default().bg(theme.selection)),
        area,
        &mut list_state,
    );
}

fn render_retrieval_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
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
        List::new(items)
            .highlight_symbol("│› ")
            .highlight_style(Style::default().bg(theme.selection)),
        results,
        &mut list_state,
    );
}

fn render_sources_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
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
                        Span::styled(
                            &source.name,
                            Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
                        ),
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
    frame.render_stateful_widget(
        List::new(items)
            .highlight_symbol("│› ")
            .highlight_style(Style::default().bg(theme.selection)),
        list,
        &mut list_state,
    );
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
                    quality.status.to_ascii_uppercase(),
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
                Line::styled("ISSUES", Style::default().fg(theme.muted)),
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
                        Span::styled(
                            &backup.id,
                            Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
                        ),
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
    frame.render_stateful_widget(
        List::new(items)
            .highlight_symbol("│› ")
            .highlight_style(Style::default().bg(theme.selection)),
        area,
        &mut list_state,
    );
}

fn render_filter_chip(
    frame: &mut Frame<'_>,
    area: Rect,
    label: &str,
    value: &str,
    accent: Color,
    theme: &Theme,
) {
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                label.to_owned(),
                Style::default().fg(accent).add_modifier(Modifier::BOLD),
            ),
            Span::styled("  ", Style::default()),
            Span::styled(value.to_owned(), Style::default().fg(theme.text)),
            Span::styled(" ›", Style::default().fg(theme.muted)),
        ]))
        .block(
            Block::default()
                .borders(Borders::BOTTOM)
                .border_style(Style::default().fg(theme.border)),
        ),
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
        let mut spans = vec![
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
        if state.model_manager.transfer_total > 0 {
            let ratio = state.model_manager.transfer_completed as f64
                / state.model_manager.transfer_total as f64;
            let filled = (ratio.clamp(0.0, 1.0) * 10.0).round() as usize;
            spans.extend([
                Span::raw("  "),
                Span::styled("█".repeat(filled), Style::default().fg(theme.green)),
                Span::styled("░".repeat(10 - filled), Style::default().fg(theme.border)),
                Span::styled(
                    format!(" {:>3.0}%", ratio * 100.0),
                    Style::default().fg(theme.yellow),
                ),
            ]);
        }
        Line::from(spans)
    } else {
        Line::from(vec![
            Span::styled("● READY", Style::default().fg(theme.green)),
            Span::styled(
                if state.model_manager.transfer_status.is_empty() {
                    "  Select an item; actions stay here, details are on the right.".into()
                } else {
                    format!("  {}", state.model_manager.transfer_status)
                },
                Style::default().fg(theme.muted),
            ),
        ])
    }
}

fn render_foundry_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    let [summary, rail, packages, status] = foundry_setup_areas(area);
    let controls = foundry_controls(state);
    let [preset_list, controls_area] = model_center_areas(packages, controls.len());
    let profile = state
        .model_manager
        .profile
        .label()
        .split(" ·")
        .next()
        .unwrap_or("Local");
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(
                    "LOCAL MODELS",
                    Style::default()
                        .fg(theme.focus)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    format!(
                        "  {} · {} · {}K",
                        profile,
                        state.model_manager.quantization.label(),
                        state.model_manager.context_tokens / 1024
                    ),
                    Style::default().fg(theme.text),
                ),
            ]),
            Line::styled(
                hardware_recommendation(metrics),
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
            role.to_ascii_uppercase(),
            Style::default()
                .fg(if configured { theme.text } else { theme.muted })
                .add_modifier(Modifier::BOLD),
        ));
    }
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(rail_spans),
            Line::styled(
                format!(
                    "{} loaded · ◆ configured · ◇ selected · ○ open",
                    metrics.loaded_models.len()
                ),
                Style::default().fg(theme.muted),
            ),
        ])
        .block(
            Block::default()
                .borders(Borders::TOP)
                .border_style(Style::default().fg(theme.border))
                .title(" Model rail "),
        ),
        rail,
    );

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
                            Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
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
    frame.render_stateful_widget(
        List::new(items)
            .highlight_symbol("│› ")
            .highlight_style(Style::default().bg(theme.selection))
            .block(
                Block::default()
                    .borders(Borders::TOP)
                    .border_style(Style::default().fg(theme.border))
                    .title(" Recommended for this device "),
            ),
        preset_list,
        &mut list_state,
    );
    render_model_center_controls(frame, controls_area, state, theme, &controls);
    frame.render_widget(
        Paragraph::new(model_transfer_line(state, metrics, theme, status.width)),
        status,
    );
}

fn render_models_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
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
        .alignment(Alignment::Right)
        .block(
            Block::default()
                .borders(Borders::BOTTOM)
                .border_style(Style::default().fg(theme.border)),
        ),
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
                        Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
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
        List::new(items)
            .highlight_symbol("│› ")
            .highlight_style(Style::default().bg(theme.selection)),
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
    frame.render_widget(Paragraph::new(status_lines), status);
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
    frame.render_widget(
        Paragraph::new(lines).block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(
                    Style::default().fg(if state.model_manager.center_controls_active {
                        theme.focus
                    } else {
                        theme.border
                    }),
                )
                .title(" Setup & actions "),
        ),
        area,
    );
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
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled("RUNTIME", Style::default().fg(theme.muted)),
            Line::from(vec![
                Span::styled("Backend     ", Style::default().fg(theme.muted)),
                Span::styled(backend, Style::default().fg(theme.text)),
            ]),
            Line::from(vec![
                Span::styled("Connection  ", Style::default().fg(theme.muted)),
                Span::styled(state.connection.label(), Style::default().fg(theme.green)),
            ]),
            Line::from(vec![
                Span::styled("CPU         ", Style::default().fg(theme.muted)),
                Span::styled(
                    format!("{} threads · {:.0}%", metrics.cpu_count, metrics.cpu_usage),
                    Style::default().fg(theme.cyan),
                ),
            ]),
            Line::from(vec![
                Span::styled("Memory      ", Style::default().fg(theme.muted)),
                Span::styled(
                    format!(
                        "{} / {}",
                        human_memory(metrics.memory_used),
                        human_memory(metrics.memory_total)
                    ),
                    Style::default().fg(theme.cyan),
                ),
            ]),
            Line::from(vec![
                Span::styled("Theme       ", Style::default().fg(theme.muted)),
                Span::styled(theme.name, Style::default().fg(theme.focus)),
            ]),
            Line::from(""),
            Line::styled(
                "All inference and retrieval services remain local.",
                Style::default().fg(theme.muted),
            ),
        ])
        .wrap(Wrap { trim: false }),
        area,
    );
}

fn render_activity_workspace(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    render_indexing_workspace(frame, area, state, theme, metrics);
}

fn render_settings_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let [intro, chat_options, editor, hints] = Layout::vertical([
        Constraint::Length(3),
        Constraint::Length(4),
        Constraint::Fill(1),
        Constraint::Length(2),
    ])
    .areas(area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled(
                "WORKSPACE CONFIGURATION",
                Style::default()
                    .fg(theme.focus)
                    .add_modifier(Modifier::BOLD),
            ),
            Line::styled(
                if state.interaction_level == InteractionLevel::Workshop {
                    "All workspace settings remain available in the advanced editor."
                } else {
                    "Safe defaults are active. Switch to Advanced to edit the full configuration."
                },
                Style::default().fg(theme.muted),
            ),
            Line::styled(
                if state.config_dirty {
                    "● Unsaved changes"
                } else {
                    "✓ Saved"
                },
                Style::default().fg(if state.config_dirty {
                    theme.yellow
                } else {
                    theme.green
                }),
            ),
        ]),
        intro,
    );
    let explain_terms = !state.bold_term_explanations_disabled;
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(
                    if explain_terms { " [✓] " } else { " [ ] " },
                    Style::default().fg(if explain_terms {
                        theme.green
                    } else {
                        theme.muted
                    }),
                ),
                Span::styled(
                    "Explain bold terms on click",
                    Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
                ),
                Span::styled("  ·  B toggle", Style::default().fg(theme.purple)),
            ]),
            Line::styled(
                "     Click a bold term to ask for a short, source-based definition.",
                Style::default().fg(theme.muted),
            ),
        ])
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(theme.border))
                .title(" Chat "),
        ),
        chat_options,
    );
    if state.interaction_level == InteractionLevel::Workshop {
        render_inline_editor(
            frame,
            editor,
            &state.config_editor,
            "Workspace YAML",
            state.input_mode == InputMode::Text,
            theme,
        );
    } else {
        frame.render_widget(
            Paragraph::new(vec![
                Line::styled(
                    "BALANCED LOCAL PROFILE",
                    Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
                ),
                Line::from(""),
                Line::from("Answers use the selected library and cite original pages."),
                Line::from("PDF processing preserves layout, tables, formulas and page anchors."),
                Line::from("Models stay local; unsupported claims are rejected in strict mode."),
                Line::from(""),
                Line::styled(
                    "Press M or Enter for Advanced when you need every Haiku setting.",
                    Style::default().fg(theme.muted),
                ),
            ])
            .wrap(Wrap { trim: false })
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme.border))
                    .title(" Active defaults "),
            ),
            editor,
        );
    }
    frame.render_widget(
        Paragraph::new(if state.interaction_level == InteractionLevel::Workshop {
            "B explain terms · Enter edit · Ctrl+Enter save"
        } else {
            "B explain terms · M / Enter advanced · Themes for colors"
        })
        .style(Style::default().fg(theme.muted)),
        hints,
    );
}

fn render_themes_workspace(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let [intro, grid, hints] = Layout::vertical([
        Constraint::Length(3),
        Constraint::Fill(1),
        Constraint::Length(2),
    ])
    .areas(area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled(
                format!("{} BUILT-IN · 1 SYSTEM PALETTE", Theme::COUNT - 1),
                Style::default()
                    .fg(theme.focus)
                    .add_modifier(Modifier::BOLD),
            ),
            Line::styled(
                "Preview instantly. Omarchy System follows your desktop automatically.",
                Style::default().fg(theme.muted),
            ),
            Line::styled(
                format!("Active · {}", Theme::at(state.theme_index).name),
                Style::default().fg(theme.cyan),
            ),
        ]),
        intro,
    );

    let columns = if grid.width >= 64 { 2 } else { 1 };
    let rows = Theme::COUNT.div_ceil(columns);
    let mut lines = Vec::with_capacity(rows);
    for row in 0..rows {
        let mut row_spans = Vec::new();
        for column in 0..columns {
            let index = row + column * rows;
            if index >= Theme::COUNT {
                continue;
            }
            if column > 0 {
                row_spans.push(Span::raw("   "));
            }
            let palette = Theme::at(index);
            let selected = index == state.theme_cursor;
            let active = index == state.theme_index && state.theme_preview_origin.is_none();
            row_spans.push(Span::styled(
                if selected { "│› " } else { "   " },
                Style::default().fg(if selected { theme.focus } else { theme.border }),
            ));
            row_spans.push(Span::styled("  ", Style::default().bg(palette.focus)));
            row_spans.push(Span::styled("  ", Style::default().bg(palette.green)));
            row_spans.push(Span::styled("  ", Style::default().bg(palette.orange)));
            row_spans.push(Span::raw(" "));
            row_spans.push(Span::styled(
                format!("{:<18}", palette.name),
                Style::default()
                    .fg(if selected { theme.focus } else { theme.text })
                    .bg(if selected {
                        theme.selection
                    } else {
                        theme.background
                    })
                    .add_modifier(if active {
                        Modifier::BOLD
                    } else {
                        Modifier::empty()
                    }),
            ));
        }
        lines.push(Line::from(row_spans));
    }
    frame.render_widget(
        Paragraph::new(lines).block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(theme.border))
                .title(" Palette library "),
        ),
        grid,
    );
    frame.render_widget(
        Paragraph::new("↑/↓ choose · Enter apply · Esc cancel · Ctrl+T next")
            .style(Style::default().fg(theme.muted)),
        hints,
    );
}

fn inspector_block(title: &str, focused: bool, theme: &Theme) -> Block<'static> {
    Block::default()
        .borders(Borders::ALL)
        .title(Line::from(vec![
            Span::styled(" ─ ", Style::default().fg(theme.focus)),
            Span::styled(
                title.to_owned(),
                Style::default()
                    .fg(theme.focus)
                    .add_modifier(Modifier::BOLD),
            ),
        ]))
        .border_style(Style::default().fg(if focused { theme.focus } else { theme.border }))
        .style(Style::default().bg(theme.surface).fg(theme.text))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FoundryControl {
    Profile,
    Quantization,
    Context,
    Memory,
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
            state
                .model_manager
                .profile
                .label()
                .split(" ·")
                .next()
                .unwrap_or("Local")
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
        FoundryControl::InstallStack => {
            let installed = state
                .model_manager
                .packages
                .get(state.model_manager.package_cursor)
                .is_some_and(|package| package.models.iter().all(|model| model.installed));
            (
                "A",
                if installed {
                    "Use stack"
                } else {
                    "Install & use"
                },
                if installed {
                    "Installed"
                } else {
                    "selected stack"
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
    let base = if selected {
        Style::default().bg(theme.selection).fg(theme.text)
    } else {
        Style::default().fg(theme.text)
    };
    Line::from(vec![
        Span::styled(if selected { "› " } else { "  " }, base),
        Span::styled(key, base.fg(accent).add_modifier(Modifier::BOLD)),
        Span::styled(format!(" {label:<14}"), base),
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
) {
    let detail_lines = match state.view {
        View::FoundryOverview => state
            .model_manager
            .packages
            .get(state.model_manager.package_cursor)
            .map_or_else(
                || {
                    vec![
                        Line::styled("NO SETUP SELECTED", Style::default().fg(theme.muted)),
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
                        Line::styled("NO MODEL SELECTED", Style::default().fg(theme.muted)),
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
    frame.render_widget(
        Paragraph::new(detail_lines)
            .wrap(Wrap { trim: false })
            .scroll((state.inspector_scroll, 0)),
        area,
    );
}

fn render_inspector(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    previews: &mut [ChatImagePreview],
) {
    let title = match state.view {
        View::Conversation | View::Retrieval => "Source",
        View::Books => "Book details",
        View::Indexing | View::Activity => "Run details",
        View::FoundryOverview => "Stack details",
        View::Models => "Model details",
        View::System => "Runtime details",
        View::Settings => "Configuration",
        View::Themes => "Palette details",
        _ => "Details",
    };
    let block = inspector_block(title, state.focus_pane == FocusPane::Inspector, theme);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if matches!(state.view, View::FoundryOverview | View::Models) {
        render_foundry_inspector(frame, inner, state, theme, metrics);
        return;
    }
    if matches!(state.view, View::Conversation | View::Retrieval) {
        render_source_inspector(frame, inner, state, theme, metrics, previews);
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
    previews: &mut [ChatImagePreview],
) {
    let [images_area, sources_area] = source_inspector_areas(area);
    let image_refs = related_image_refs(state);
    let images_block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(theme.border))
        .title(Line::styled(
            " Related images · 4 ",
            Style::default()
                .fg(theme.purple)
                .add_modifier(Modifier::BOLD),
        ));
    let images_inner = images_block.inner(images_area);
    frame.render_widget(images_block, images_area);
    if image_refs.is_empty() {
        frame.render_widget(
            Paragraph::new("Ask a question to find matching pages and figures.")
                .style(Style::default().fg(theme.muted))
                .alignment(Alignment::Center)
                .wrap(Wrap { trim: true }),
            images_inner,
        );
    } else {
        let [top, bottom] =
            Layout::vertical([Constraint::Percentage(50), Constraint::Percentage(50)])
                .areas(images_inner);
        let [top_left, top_right] =
            Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)]).areas(top);
        let [bottom_left, bottom_right] =
            Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)])
                .areas(bottom);
        let tiles = [top_left, top_right, bottom_left, bottom_right];
        for (slot, tile) in tiles.into_iter().enumerate() {
            let Some((citation_index, page_index, page)) = image_refs.get(slot).copied() else {
                frame.render_widget(
                    Block::default()
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme.border)),
                    tile,
                );
                continue;
            };
            let selected =
                citation_index == state.citation_cursor && page_index == state.citation_page_cursor;
            let tile_block = Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(if selected {
                    theme.focus
                } else {
                    theme.border
                }))
                .title(Line::styled(
                    format!(" {} · p.{page} ", slot + 1),
                    Style::default().fg(if selected { theme.focus } else { theme.muted }),
                ));
            let tile_inner = tile_block.inner(tile);
            frame.render_widget(tile_block, tile);
            if let Some(preview) = previews.iter_mut().find(|preview| {
                preview.citation_index == citation_index && preview.page_index == page_index
            }) {
                preview.receive_resizes();
                frame.render_stateful_widget(
                    StatefulImage::default(),
                    tile_inner,
                    &mut preview.protocol,
                );
            } else {
                let source = state.chat.citations[citation_index]
                    .document_title
                    .as_deref()
                    .unwrap_or("Source");
                frame.render_widget(
                    Paragraph::new(vec![
                        Line::styled(
                            truncate(source, tile_inner.width.saturating_sub(1) as usize),
                            Style::default().fg(theme.text),
                        ),
                        Line::styled("Rendering preview…", Style::default().fg(theme.muted)),
                    ])
                    .alignment(Alignment::Center)
                    .wrap(Wrap { trim: true }),
                    tile_inner,
                );
            }
        }
    }

    let sources_block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(theme.border))
        .title(Line::styled(
            " Sources ",
            Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
        ));
    let sources_inner = sources_block.inner(sources_area);
    frame.render_widget(sources_block, sources_area);
    let lines = inspector_lines(state, theme, metrics, sources_inner.width);
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .scroll((state.inspector_scroll, 0)),
        sources_inner,
    );
}

pub(crate) fn source_inspector_areas(area: Rect) -> [Rect; 2] {
    let image_height = (area.height * 45 / 100).clamp(10, 22).min(area.height);
    Layout::vertical([Constraint::Length(image_height), Constraint::Fill(1)]).areas(area)
}

pub fn related_image_refs(state: &AppState) -> Vec<(usize, usize, u32)> {
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
    for citation_index in 0..state.chat.citations.len() {
        if !state.chat.citations[citation_index].picture_refs.is_empty() {
            consider(citation_index, 0);
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

pub(crate) fn source_citation_row_offset(state: &AppState) -> u16 {
    let receipt_rows = receipt_lines(state, &Theme::default()).len() as u16;
    receipt_rows
        .saturating_add(u16::from(receipt_rows > 0))
        .saturating_add(3)
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
                let mut lines = receipt_lines(state, theme);
                if !lines.is_empty() {
                    lines.push(Line::from(""));
                }
                lines.extend([
                    Line::styled("NO SOURCE SELECTED", Style::default().fg(theme.muted)),
                    Line::from(""),
                    Line::styled(
                        if state.chat.receipt.is_some() {
                            "No usable source supported this answer."
                        } else {
                            "Ask a question. Sources and page anchors will appear here."
                        },
                        Style::default().fg(theme.muted),
                    ),
                ]);
                return lines;
            }
            let mut lines = receipt_lines(state, theme);
            if !lines.is_empty() {
                lines.push(Line::from(""));
            }
            lines.extend([
                Line::styled(
                    format!("{} SOURCES", state.chat.citations.len()),
                    Style::default().fg(theme.muted),
                ),
                Line::styled(
                    format!("Evidence mode · {}", state.chat.evidence_mode),
                    Style::default().fg(theme.cyan),
                ),
                Line::from(""),
            ]);
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
                            if selected { "│" } else { " " },
                            citation.evidence_id.as_deref().unwrap_or("E?")
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
            lines.push(Line::styled(
                "↑↓ source · ←→ page · Enter / click open",
                Style::default().fg(theme.muted),
            ));
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
                Line::styled(
                    document.title.clone(),
                    Style::default()
                        .fg(theme.focus)
                        .add_modifier(Modifier::BOLD),
                ),
                Line::from(""),
                Line::styled("SOURCE", Style::default().fg(theme.muted)),
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
                Line::from(format!("Tags       {tags}")),
                Line::from(""),
                Line::styled(
                    "Enter open · N info · T tags",
                    Style::default().fg(theme.muted),
                ),
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
            vec![
                Line::styled(
                    job.kind.to_ascii_uppercase(),
                    Style::default()
                        .fg(theme.focus)
                        .add_modifier(Modifier::BOLD),
                ),
                Line::from(""),
                Line::from(format!("Status     {:?}", job.status)),
                Line::from(format!("Progress   {:.0}%", job.progress * 100.0)),
                Line::from(format!("Phase      {}", job.phase)),
                Line::from(format!("Updated    {}", job.updated_at)),
                Line::from(""),
                Line::styled(
                    "Space pause/resume · X cancel",
                    Style::default().fg(theme.muted),
                ),
            ]
        }
        View::Models => {
            let Some(entry) = state.model_manager.entries.get(state.model_manager.cursor) else {
                return vec![
                    Line::styled("MODEL DETAILS", Style::default().fg(theme.muted)),
                    Line::from(""),
                    Line::styled(
                        "Load the catalog and select a model.",
                        Style::default().fg(theme.muted),
                    ),
                ];
            };
            vec![
                Line::styled(
                    entry.id.clone(),
                    Style::default()
                        .fg(theme.focus)
                        .add_modifier(Modifier::BOLD),
                ),
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
                Line::styled("LOCAL RUNTIME", Style::default().fg(theme.muted)),
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
                Line::styled("RESIDENT MODELS", Style::default().fg(theme.muted)),
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
                    Line::styled(
                        source.name.clone(),
                        Style::default()
                            .fg(theme.focus)
                            .add_modifier(Modifier::BOLD),
                    ),
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
                    "UNSAVED"
                } else {
                    "SAVED"
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
                Line::styled(
                    palette.name,
                    Style::default()
                        .fg(theme.focus)
                        .add_modifier(Modifier::BOLD),
                ),
                Line::from(""),
                Line::styled("LIVE PREVIEW", Style::default().fg(theme.muted)),
                Line::from(vec![
                    Span::styled(
                        "  FOCUS  ",
                        Style::default().fg(theme.background).bg(palette.focus),
                    ),
                    Span::raw("  "),
                    Span::styled(
                        "  READY  ",
                        Style::default().fg(theme.background).bg(palette.green),
                    ),
                ]),
                Line::from(vec![
                    Span::styled(
                        " WARNING ",
                        Style::default().fg(theme.background).bg(palette.yellow),
                    ),
                    Span::raw("  "),
                    Span::styled(
                        "  ERROR  ",
                        Style::default().fg(theme.background).bg(palette.red),
                    ),
                ]),
                Line::from(""),
                Line::styled(
                    "Enter keeps this palette. Esc restores the previous one.",
                    Style::default().fg(theme.muted),
                ),
            ];
            if state.theme_cursor == Theme::COUNT - 1 {
                lines.extend([
                    Line::from(""),
                    Line::styled(
                        if Theme::omarchy_available() {
                            "AUTO · SYSTEM COLORS ACTIVE"
                        } else {
                            "AUTO · SAFE FALLBACK ACTIVE"
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
        Line::styled("ANSWER CHECK", Style::default().fg(theme.muted)),
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
    centered(54, 9, screen)
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
                    spans.push(Span::styled(
                        prefix,
                        Style::default()
                            .fg(theme.focus)
                            .add_modifier(Modifier::BOLD),
                    ));
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

fn shortcut_words(theme: &Theme, words: &[(&str, char, Color)]) -> Line<'static> {
    let mut spans = Vec::new();
    for (index, (word, key, color)) in words.iter().enumerate() {
        if index > 0 {
            spans.push(Span::raw("   "));
        }
        spans.extend(shortcut_word(theme, word, *key, *color));
    }
    Line::from(spans)
}

fn shortcut_word(theme: &Theme, word: &str, key: char, color: Color) -> Vec<Span<'static>> {
    let lower = word.to_ascii_lowercase();
    let needle = key.to_ascii_lowercase().to_string();
    let position = lower.find(&needle).unwrap_or_default();
    let end = position + key.len_utf8();
    vec![
        Span::styled(word[..position].to_owned(), Style::default().fg(theme.text)),
        Span::styled(
            word[position..end].to_owned(),
            Style::default().fg(color).add_modifier(Modifier::BOLD),
        ),
        Span::styled(word[end..].to_owned(), Style::default().fg(theme.text)),
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
                    ("B", "Explain terms"),
                    ("M", "Simple/advanced"),
                    ("Enter", "Edit"),
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
                theme.cyan
            })
            .add_modifier(Modifier::BOLD),
    )];
    for (index, (key, label)) in hints.iter().enumerate() {
        spans.push(Span::styled(
            format!("  {key}"),
            Style::default()
                .fg([
                    theme.purple,
                    theme.cyan,
                    theme.green,
                    theme.orange,
                    theme.yellow,
                    theme.red,
                ][index % 6])
                .add_modifier(Modifier::BOLD),
        ));
        spans.push(Span::raw(format!(" {label}")));
    }
    if state.undo.is_some() {
        spans.push(Span::styled(
            "  Ctrl+Z",
            Style::default()
                .fg(theme.green)
                .add_modifier(Modifier::BOLD),
        ));
        spans.push(Span::raw(" Undo"));
    }
    frame.render_widget(
        Paragraph::new(Line::from(spans)).style(Style::default().fg(theme.muted).bg(theme.panel)),
        area,
    );
}

fn render_overlay(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    match state.overlay {
        Some(Overlay::ConfirmQuit) => render_confirm_quit(frame, theme),
        Some(Overlay::Palette) => render_palette(frame, state, theme),
        Some(Overlay::Workspaces) => render_libraries(frame, state, theme),
        Some(Overlay::Help) => render_help(frame, theme),
        Some(Overlay::ConfirmModelDelete) => render_delete_model_confirm(frame, state, theme),
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
        Some(Overlay::DocumentTags) => render_document_tags(frame, state, theme),
        Some(Overlay::CustomModel) => render_custom_model(frame, state, theme),
        None => {}
    }
}

fn render_confirm_quit(frame: &mut Frame<'_>, theme: &Theme) {
    let area = confirm_quit_area(frame.area());
    frame.render_widget(Clear, area);
    let block = panel("Really quit?", true, theme);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled(
                "Close OmaRag?",
                Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
            ),
            Line::styled(
                "Answers and local data are already saved.",
                Style::default().fg(theme.muted),
            ),
            Line::from(""),
            Line::from(vec![
                Span::styled(
                    " Enter / Y  Quit ",
                    Style::default()
                        .fg(theme.background)
                        .bg(theme.red)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw("    "),
                Span::styled(
                    " Esc / N  Keep working ",
                    Style::default()
                        .fg(theme.background)
                        .bg(theme.green)
                        .add_modifier(Modifier::BOLD),
                ),
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
                Style::default()
                    .fg(theme.focus)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled("   [ ] Role ", Style::default().fg(theme.cyan)),
            Span::styled(
                state.model_manager.category.label(),
                Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
            ),
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
    frame.render_widget(panel("Import airlock", true, theme), area);

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
            Span::styled(
                if entry.is_dir { "▸ " } else { "PDF " },
                Style::default().fg(if entry.is_dir {
                    theme.yellow
                } else {
                    theme.cyan
                }),
            ),
            Span::styled(&entry.name, Style::default().fg(theme.text)),
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
    let path = truncate(
        &state.file_browser.current_dir,
        list_area.width.saturating_sub(4) as usize,
    );
    frame.render_stateful_widget(
        List::new(entries)
            .block(panel(&path, true, theme))
            .highlight_symbol("›")
            .highlight_style(
                Style::default()
                    .bg(theme.selection)
                    .fg(theme.cyan)
                    .add_modifier(Modifier::BOLD),
            ),
        list_area,
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
        selected.push(ListItem::new(Line::styled(
            " ★ FAVORITES",
            Style::default()
                .fg(theme.yellow)
                .add_modifier(Modifier::BOLD),
        )));
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
        selected.push(ListItem::new(Line::styled(
            " ↺ RECENT",
            Style::default()
                .fg(theme.purple)
                .add_modifier(Modifier::BOLD),
        )));
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
    frame.render_widget(
        List::new(selected).block(panel("Selected", false, theme)),
        selected_area,
    );
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(
                    "Open",
                    Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
                ),
                Span::raw("    "),
                Span::styled(
                    "Toggle",
                    Style::default()
                        .fg(theme.green)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw("    "),
                Span::styled(
                    "Import",
                    Style::default()
                        .fg(theme.orange)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw("    "),
                Span::styled(
                    "Cancel",
                    Style::default().fg(theme.red).add_modifier(Modifier::BOLD),
                ),
            ]),
            Line::styled(
                "← parent  → open  Space select  Enter review  F favorite  R recent  Esc close",
                Style::default().fg(theme.muted),
            ),
        ]),
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
            Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
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
            "CONFIRM DETECTED BOOK IDENTITY",
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
            Span::styled("Status       ", Style::default().fg(theme.muted)),
            Span::raw(&document.status),
        ]),
        Line::from(vec![
            Span::styled("Pages        ", Style::default().fg(theme.muted)),
            Span::raw(
                document
                    .page_count
                    .or_else(|| detail.and_then(|item| item.pages))
                    .map_or("scanning".into(), |pages| pages.to_string()),
            ),
        ]),
        Line::from(vec![
            Span::styled("Edition      ", Style::default().fg(theme.muted)),
            Span::raw(
                document
                    .book
                    .as_ref()
                    .and_then(|book| book.edition_label.as_deref())
                    .unwrap_or("not confirmed"),
            ),
        ]),
        Line::from(vec![
            Span::styled("Authors      ", Style::default().fg(theme.muted)),
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
            Span::styled("ISBN         ", Style::default().fg(theme.muted)),
            Span::raw(document.book.as_ref().map_or("—".into(), |book| {
                if book.isbn.is_empty() {
                    "—".into()
                } else {
                    book.isbn.join(", ")
                }
            })),
        ]),
        Line::from(vec![
            Span::styled("Parser       ", Style::default().fg(theme.muted)),
            Span::raw(format!("{} · structure-aware Hybrid", document.parser_id)),
        ]),
        Line::from(vec![
            Span::styled("Conversion   ", Style::default().fg(theme.muted)),
            Span::raw(document.cache_status.as_deref().unwrap_or("legacy")),
        ]),
        Line::from(vec![
            Span::styled("Pipeline     ", Style::default().fg(theme.muted)),
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
            Span::styled("Size         ", Style::default().fg(theme.muted)),
            Span::raw(detail.map_or("scanning".into(), |item| format_bytes(item.size_bytes))),
        ]),
        Line::from(vec![
            Span::styled("Chunks       ", Style::default().fg(theme.muted)),
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
            Span::styled("Provenance   ", Style::default().fg(theme.muted)),
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
            Span::styled("Imported     ", Style::default().fg(theme.muted)),
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
            Span::styled("Embedding    ", Style::default().fg(theme.muted)),
            Span::raw(
                config_model(
                    state.config.as_ref().map_or("", |config| &config.content),
                    "embeddings",
                )
                .unwrap_or_else(|| "Haiku default".into()),
            ),
        ]),
        Line::from(vec![
            Span::styled("Tags         ", Style::default().fg(theme.muted)),
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
    frame.render_stateful_widget(
        List::new(items)
            .highlight_symbol("›")
            .highlight_style(Style::default().bg(theme.selection).fg(theme.orange))
            .block(
                Block::default()
                    .borders(Borders::RIGHT)
                    .border_style(Style::default().fg(theme.border)),
            ),
        list,
        &mut list_state,
    );
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
                    ("Apply", 'A', theme.green),
                    ("Custom", 'C', theme.cyan),
                    ("Edit", 'E', theme.yellow),
                    ("Back", 'B', theme.red),
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
            shortcut_words(
                theme,
                &[("Save", 'S', theme.green), ("Cancel", 'C', theme.red)],
            ),
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
                "Enter removes the library from Oracle but keeps every file.",
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
                    ("Unregister", 'U', theme.green),
                    ("Delete permanently", 'D', theme.red),
                    ("Cancel", 'C', theme.muted),
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
            .highlight_symbol("›")
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
        Paragraph::new("Comma-separated tags are local to Oracle and searchable with /."),
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
                format!("STACK #{}  {}", package.recommended_rank, package.name),
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
            "Cross-encoder files cache automatically on first use.",
            Style::default().fg(theme.muted),
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
            Span::styled("GPU       ", Style::default().fg(theme.muted)),
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

fn render_palette(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let commands = filtered_palette_commands(state);
    let area = centered(64, (commands.len() as u16 + 5).clamp(9, 21), frame.area());
    frame.render_widget(Clear, area);
    let [input, list] = Layout::vertical([Constraint::Length(3), Constraint::Fill(1)]).areas(area);
    render_inline_editor(
        frame,
        input,
        &state.palette.query,
        "Command · Enter run · Esc close",
        true,
        theme,
    );
    let items = commands
        .iter()
        .map(|command| ListItem::new(command.label()));
    let mut list_state = ListState::default();
    if !commands.is_empty() {
        list_state.select(Some(state.palette.cursor.min(commands.len() - 1)));
    }
    frame.render_stateful_widget(
        List::new(items)
            .highlight_symbol("›")
            .highlight_style(Style::default().bg(theme.selection).fg(theme.cyan))
            .block(panel("Commands", true, theme)),
        list,
        &mut list_state,
    );
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
                shortcut_words(
                    theme,
                    &[("Profile", 'P', theme.orange), ("Custom", 'C', theme.cyan)],
                ),
                Line::styled(
                    "Alt+P profiles   Alt+C custom",
                    Style::default().fg(theme.muted),
                ),
            ])
            .block(
                Block::default()
                    .borders(Borders::TOP)
                    .border_style(Style::default().fg(theme.border)),
            ),
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
                Span::styled(
                    &workspace.name,
                    Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
                ),
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
        &[("New library", 'N', theme.green)],
    )));
    items.push(ListItem::new(shortcut_words(
        theme,
        &[("Delete selected library", 'D', theme.red)],
    )));
    let mut list_state = ListState::default();
    if !state.workspaces.is_empty() {
        list_state.select(Some(state.workspace_cursor));
    }
    frame.render_stateful_widget(
        List::new(items)
            .highlight_symbol("›")
            .highlight_style(Style::default().bg(theme.selection).fg(theme.cyan))
            .block(panel(
                "Libraries · arrows · Enter · N new · D delete",
                true,
                theme,
            )),
        area,
        &mut list_state,
    );
}

fn render_help(frame: &mut Frame<'_>, theme: &Theme) {
    let area = centered(68, 24, frame.area());
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled(
                "Movement",
                Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
            ),
            Line::from("Tab / Shift+Tab   Sidebar / Workspace / Inspector"),
            Line::from("Arrow keys         act on the focused pane"),
            Line::from("Enter              open, edit or pause/resume"),
            Line::from("Esc                leave text input"),
            Line::from("Mouse click/wheel   focus, activate and scroll"),
            Line::from("Right / middle      back / cycle theme"),
            Line::from(""),
            Line::styled(
                "Ctrl shortcuts",
                Style::default()
                    .fg(theme.purple)
                    .add_modifier(Modifier::BOLD),
            ),
            Line::from("Ctrl+C Quit         Ctrl+S Sources      Ctrl+H Presets"),
            Line::from("Ctrl+M Catalog      Ctrl+A Indexing     Ctrl+T Theme"),
            Line::from("Ctrl+L Libraries    Ctrl+P Palette      Ctrl+Q Quit"),
            Line::from("Ctrl+I Add PDFs     N New library      Ctrl+E Evidence mode"),
            Line::from("Ctrl+R Refresh      Ctrl+X Stop         Ctrl+D Clear"),
            Line::from("/ Active search     : Command palette  ? Help"),
            Line::from(""),
            Line::styled(
                "Text input",
                Style::default()
                    .fg(theme.yellow)
                    .add_modifier(Modifier::BOLD),
            ),
            Line::from("Arrows/Home/End     move cursor"),
            Line::from("Backspace/Delete    edit text"),
            Line::from("Enter               submit"),
            Line::from(""),
            Line::from("? / Enter / Esc     close help"),
        ])
        .wrap(Wrap { trim: false })
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
    let block = Block::default()
        .borders(Borders::TOP)
        .title(format!(" {label} "))
        .title_style(Style::default().fg(if focused { theme.yellow } else { theme.muted }))
        .border_style(Style::default().fg(if focused { theme.focus } else { theme.border }));
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

fn panel<'a>(title: &'a str, focused: bool, theme: &Theme) -> Block<'a> {
    let accent = panel_accent(title, theme);
    let title_style = if focused {
        Style::default()
            .fg(theme.background)
            .bg(accent)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(accent).add_modifier(Modifier::BOLD)
    };
    Block::default()
        .borders(Borders::ALL)
        .border_type(if focused {
            BorderType::Thick
        } else {
            BorderType::Plain
        })
        .title(if focused {
            format!(" ◆ {title} ")
        } else {
            format!(" {title} ")
        })
        .title_style(title_style)
        .border_style(Style::default().fg(if focused { accent } else { theme.border }))
        .style(Style::default().bg(theme.panel).fg(theme.text))
}

fn panel_accent(title: &str, theme: &Theme) -> Color {
    if title.starts_with("Nav") {
        theme.purple
    } else if title.starts_with("Chat") {
        theme.cyan
    } else if title.starts_with("Library") || title.starts_with("Import") {
        theme.orange
    } else if title.starts_with("Hardware") {
        theme.green
    } else if title.starts_with("Models") {
        theme.purple
    } else if title.starts_with("Activity") {
        theme.yellow
    } else {
        theme.focus
    }
}

fn activity_item<'a>(job: &'a JobSnapshot, theme: &Theme) -> ListItem<'a> {
    let color = job_color(job, theme);
    ListItem::new(vec![
        Line::from(vec![
            Span::styled(
                format!(" {} ", status_symbol(&job.status)),
                Style::default().fg(color),
            ),
            Span::styled(
                &job.kind,
                Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                format!("  {:.0}%", job.progress * 100.0),
                Style::default().fg(color),
            ),
        ]),
        Line::styled(
            format!("   {}", truncate(&job.phase, 34)),
            Style::default().fg(theme.muted),
        ),
    ])
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

fn model_role_lines(
    role: &str,
    model: Option<&str>,
    residency: &str,
    width: u16,
    theme: &Theme,
) -> [Line<'static>; 2] {
    let (marker, color) = match residency {
        "active" => ("▶", theme.orange),
        "loaded" => ("●", theme.green),
        "loading" => ("◐", theme.yellow),
        "idle" => ("◇", theme.cyan),
        _ => ("○", theme.muted),
    };
    let role = match role {
        "embedding" => "Embed",
        "rerank" => "Rerank",
        "chat" => "Chat",
        "vl" => "VL",
        other => other,
    };
    let model = model.map_or("unset".into(), compact_model_name);
    [
        Line::from(vec![
            Span::styled(format!(" {marker} "), Style::default().fg(color)),
            Span::styled(role.to_owned(), Style::default().fg(theme.muted)),
        ]),
        Line::styled(
            format!("   {}", truncate(&model, width.saturating_sub(5) as usize)),
            Style::default().fg(if model == "unset" {
                theme.muted
            } else {
                theme.text
            }),
        ),
    ]
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

fn status_symbol(status: &JobStatus) -> &'static str {
    match status {
        JobStatus::Queued => "○",
        JobStatus::Running => "▶",
        JobStatus::PauseRequested | JobStatus::Paused => "‖",
        JobStatus::Completed => "✓",
        JobStatus::Cancelled => "×",
        JobStatus::Failed => "!",
    }
}

fn is_terminal(status: &JobStatus) -> bool {
    matches!(
        status,
        JobStatus::Completed | JobStatus::Cancelled | JobStatus::Failed
    )
}
fn spinner(tick: u64) -> &'static str {
    ["◐", "◓", "◑", "◒"][(tick as usize / 2) % 4]
}

fn human_memory(bytes: u64) -> String {
    const GIB: f64 = 1_073_741_824.0;
    const MIB: f64 = 1_048_576.0;
    if bytes as f64 >= GIB {
        format!("{:.1} GiB", bytes as f64 / GIB)
    } else {
        format!("{:.0} MiB", bytes as f64 / MIB)
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
        format!("{:.0} KiB", bytes as f64 / 1024.0)
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
    fn wide_shell_contains_sidebar_workspace_and_inspector() {
        let content = rendered(160, 42, &AppState::default(), Theme::default());
        for title in [
            "CHAT",
            "LIBRARY",
            "MODELS",
            "SETTINGS",
            "Conversation",
            "Source",
            "INSTRUMENTS",
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
        let cpu = rows.iter().position(|row| row.contains("CPU"));
        let ram = rows.iter().position(|row| row.contains("RAM"));
        let vram = rows.iter().position(|row| row.contains("VRAM"));
        assert!(cpu.is_some() && ram.is_some() && vram.is_some());
        assert_ne!(cpu, ram);
        assert_ne!(ram, vram);
        assert_ne!(cpu, vram);
        let chat = rows.iter().position(|row| row.contains("Chat")).unwrap();
        let model = rows
            .iter()
            .position(|row| row.contains("qwen3.5:4b"))
            .unwrap();
        assert_eq!(model, chat + 1);
    }

    #[test]
    fn all_themes_are_visibly_distinct() {
        let themes = (0..Theme::COUNT).map(Theme::at).collect::<Vec<_>>();
        assert_eq!(
            themes
                .iter()
                .map(|theme| theme.name)
                .collect::<std::collections::BTreeSet<_>>()
                .len(),
            Theme::COUNT
        );
        assert_eq!(
            themes
                .iter()
                .map(|theme| theme.background)
                .collect::<std::collections::HashSet<_>>()
                .len(),
            Theme::COUNT
        );
        assert!(themes.iter().all(|theme| theme.focus != theme.border));
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
    fn omarag_header_has_identity_companions_and_no_connection_badge() {
        let state = AppState::default();
        let idle = rendered_metrics(
            160,
            42,
            &state,
            Theme::default(),
            &RuntimeMetrics::default(),
        );
        let mut active_state = AppState::default();
        active_state.chat.request_pending = true;
        let active = rendered_metrics(
            160,
            42,
            &active_state,
            Theme::default(),
            &RuntimeMetrics {
                animation_tick: 6,
                ..RuntimeMetrics::default()
            },
        );
        assert!(idle.contains("OmaRag"));
        assert!(idle.contains("ORACLE OF METIS & ALETHEIA"));
        assert!(idle.contains("Metis"));
        assert!(idle.contains("Aletheia"));
        assert!(idle.contains("Conversation"));
        assert!(!idle.contains("CONNECTED"));
        assert!(!idle.contains("CONNECTING"));
        assert_ne!(idle, active);
    }

    #[test]
    fn metis_and_aletheia_have_distinct_animation_frames() {
        assert_ne!(companion_poses(0), companion_poses(2));
        assert_ne!(companion_poses(2), companion_poses(4));
        assert!(companion_poses(0).0.contains('◆'));
        assert!(companion_poses(0).1.contains('◇'));
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
        assert_ne!(theme.surface, theme.background);
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
        assert!(compact.contains("Metis"));
        assert!(compact.contains("Aletheia"));
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
        assert!(content.contains("Minimum: 80×24"));
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
                Some(View::Books),
                Some(View::Books),
                Some(View::Indexing),
                Some(View::FoundryOverview),
                Some(View::FoundryOverview),
                Some(View::Models),
                Some(View::Settings),
                Some(View::Settings),
                Some(View::Themes),
            ]
        );
        let advanced = AppState {
            interaction_level: InteractionLevel::Workshop,
            ..AppState::default()
        };
        assert_eq!(sidebar_navigation_rows(&advanced).len(), 17);
    }

    #[test]
    fn all_visible_panes_have_complete_frames() {
        let content = rendered(160, 42, &AppState::default(), Theme::default());
        assert!(content.matches('┌').count() >= 3);
        assert!(content.matches('┘').count() >= 3);
    }

    #[test]
    fn theme_action_cycles_at_runtime() {
        let mut state = AppState::default();
        update(&mut state, Action::CycleTheme);
        assert_eq!(Theme::at(state.theme_index).name, "One Dark");
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
            "Model rail",
            "Recommended for this device",
            "Qwen Unified",
            "Qwen retrieval family",
            "Setup & actions",
            "Install & use",
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
        assert!(!medium_inspector.contains("Setup & actions"));

        state.focus_pane = FocusPane::Workspace;
        let narrow_workspace = rendered(80, 24, &state, Theme::default());
        assert!(narrow_workspace.contains("Model rail"));
        state.focus_pane = FocusPane::Inspector;
        let narrow_inspector = rendered(80, 24, &state, Theme::default());
        assert!(narrow_inspector.contains("Stack details"));
        assert!(!narrow_inspector.contains("Setup & actions"));
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
