pub mod input;

use input::{filtered_palette_commands, fuzzy_score};
#[cfg(test)]
use omarag_app::ModelQuantization;
use omarag_app::{
    AppState, EditorState, FocusPanel, InputMode, LibraryFilter, LibrarySort, ModelCatalogEntry,
    ModelCategory, ModelFit, ModelPackage, ModelSource, NotificationLevel, Overlay,
    WorkspaceProfile,
};
use omarag_domain::{JobSnapshot, JobStatus};
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
use unicode_width::UnicodeWidthStr;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Theme {
    pub name: &'static str,
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
}

impl Theme {
    pub const COUNT: usize = 4;

    pub const fn at(index: usize) -> Self {
        match index % Self::COUNT {
            0 => Self {
                name: "Blueprint",
                background: rgb(0x11111B),
                panel: rgb(0x1E1E2E),
                text: rgb(0xCDD6F4),
                muted: rgb(0xA6ADC8),
                border: rgb(0x585B70),
                focus: rgb(0xF38BA8),
                cyan: rgb(0x89DCEB),
                green: rgb(0xA6E3A1),
                yellow: rgb(0xF9E2AF),
                red: rgb(0xF38BA8),
                purple: rgb(0xCBA6F7),
                orange: rgb(0xFAB387),
                selection: rgb(0x313244),
            },
            1 => Self {
                name: "Nord Harbor",
                background: rgb(0x2E3440),
                panel: rgb(0x3B4252),
                text: rgb(0xECEFF4),
                muted: rgb(0xD8DEE9),
                border: rgb(0x4C566A),
                focus: rgb(0x88C0D0),
                cyan: rgb(0x8FBCBB),
                green: rgb(0xA3BE8C),
                yellow: rgb(0xEBCB8B),
                red: rgb(0xBF616A),
                purple: rgb(0xB48EAD),
                orange: rgb(0xD08770),
                selection: rgb(0x434C5E),
            },
            2 => Self {
                name: "Warm Workshop",
                background: rgb(0x1A1814),
                panel: rgb(0x28241E),
                text: rgb(0xF2E6D0),
                muted: rgb(0xB9A98D),
                border: rgb(0x5C5245),
                focus: rgb(0xF0A868),
                cyan: rgb(0x77C4C9),
                green: rgb(0x98C379),
                yellow: rgb(0xE5C07B),
                red: rgb(0xE06C75),
                purple: rgb(0xC678DD),
                orange: rgb(0xD19A66),
                selection: rgb(0x3A332A),
            },
            _ => Self {
                name: "Paper Plan",
                background: rgb(0xF4F1E8),
                panel: rgb(0xE8E2D5),
                text: rgb(0x263238),
                muted: rgb(0x607D8B),
                border: rgb(0xA8A295),
                focus: rgb(0x176B87),
                cyan: rgb(0x00838F),
                green: rgb(0x2E7D32),
                yellow: rgb(0xA15C00),
                red: rgb(0xC62828),
                purple: rgb(0x6A1B9A),
                orange: rgb(0xE65100),
                selection: rgb(0xD8E4E7),
            },
        }
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

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LoadedModel {
    pub name: String,
    pub size: u64,
    pub size_vram: u64,
    pub context_length: u64,
    pub parameter_size: String,
    pub quantization: String,
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
}

pub struct ChatImagePreview {
    pub pdf_path: String,
    pub page: u32,
    pub title: String,
    pub protocol: ThreadProtocol,
    response_rx: std::sync::mpsc::Receiver<ResizeResponse>,
}

impl ChatImagePreview {
    pub fn new(pdf_path: String, page: u32, title: String, protocol: StatefulProtocol) -> Self {
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
    let [header, body, footer] = screen_areas(frame.area());
    render_header(frame, header, state, theme, metrics);
    let [chat, library, compute, activity] = dashboard_areas(body);
    render_chat(frame, chat, state, theme, metrics, previews);
    render_library(frame, library, state, theme);
    render_compute(frame, compute, state, theme, metrics);
    render_activity(frame, activity, state, theme, metrics);
    render_footer(frame, footer, state, theme);
    render_overlay(frame, state, theme, metrics);
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
    let pulse_color = if ollama_active {
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
    if area.width < 110 {
        let mut import = shortcut_words(theme, &[("Index new PDFs", 'I', theme.orange)]);
        import.spans.insert(0, Span::raw("  "));
        import.spans.push(Span::styled(
            "  Enter / I",
            Style::default().fg(theme.muted),
        ));
        frame.render_widget(
            Paragraph::new(vec![
                Line::styled(
                    " ◐ ORACLE OF DÆDALUS",
                    Style::default()
                        .fg(pulse_color)
                        .add_modifier(Modifier::BOLD),
                ),
                Line::styled(
                    truncate(
                        " OFFLINE RETRIEVAL-AUGMENTED COMMAND-LINE ENVIRONMENT",
                        inner.width as usize,
                    ),
                    Style::default().fg(theme.muted),
                ),
                import,
            ]),
            inner,
        );
        return;
    }
    // Keep the call-to-action on the exact same axis as Library and Activity.
    let [wordmark, signal] =
        Layout::horizontal([Constraint::Percentage(67), Constraint::Percentage(33)]).areas(inner);
    let mirror_left = if ollama_active && pulse_phase.is_multiple_of(2) {
        pulse_color
    } else {
        theme.green
    };
    let mirror_right = if ollama_active && !pulse_phase.is_multiple_of(2) {
        pulse_color
    } else {
        theme.cyan
    };
    let logo_style = Style::default()
        .fg(theme.green)
        .add_modifier(Modifier::BOLD);
    let mirror_style = |color| Style::default().fg(color).add_modifier(Modifier::BOLD);

    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(" ▟██▌", mirror_style(mirror_left)),
                Span::styled("▐██▙", mirror_style(mirror_right)),
                Span::styled("  ████▙  ▟███▙  ▟████  ██     █████", logo_style),
            ]),
            Line::from(vec![
                Span::styled(" ██  ", mirror_style(mirror_left)),
                Span::styled("  ██", mirror_style(mirror_right)),
                Span::styled("  ████▘  █████  ██     ██     ████ ", logo_style),
            ]),
            Line::from(vec![
                Span::styled(" ▜██▌", mirror_style(mirror_left)),
                Span::styled("▐██▛", mirror_style(mirror_right)),
                Span::styled("  ██ ▜▙  ██ ██  ▜████  █████  █████", logo_style),
            ]),
            Line::from(vec![
                Span::styled(
                    " OF DÆDALUS",
                    Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
                ),
                Span::styled("  //  ", Style::default().fg(theme.border)),
                Span::styled(
                    "OFFLINE RETRIEVAL-AUGMENTED COMMAND-LINE ENVIRONMENT",
                    Style::default().fg(theme.muted),
                ),
            ]),
        ]),
        wordmark,
    );
    let block = shortcut_panel(
        "Index new PDFs",
        'I',
        state.focus == FocusPanel::Import,
        theme,
    );
    let button = block.inner(signal);
    frame.render_widget(block, signal);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled("PDF or folder", Style::default().fg(theme.text)),
            Line::styled("Enter / I", Style::default().fg(theme.muted)),
        ])
        .alignment(Alignment::Center),
        button,
    );
}

pub(crate) fn screen_areas(area: Rect) -> [Rect; 3] {
    Layout::vertical([
        Constraint::Length(5),
        Constraint::Fill(1),
        Constraint::Length(1),
    ])
    .areas(area)
}

pub(crate) fn dashboard_areas(area: Rect) -> [Rect; 4] {
    let [top, bottom] =
        Layout::vertical([Constraint::Percentage(66), Constraint::Percentage(34)]).areas(area);
    let columns = [Constraint::Percentage(67), Constraint::Percentage(33)];
    let [chat, library] = Layout::horizontal(columns).areas(top);
    let [compute, activity] = Layout::horizontal(columns).areas(bottom);
    [chat, library, compute, activity]
}

pub(crate) fn header_import_area(area: Rect) -> Rect {
    if area.width < 110 {
        Rect::new(
            area.x,
            area.y.saturating_add(2),
            area.width,
            1.min(area.height),
        )
    } else {
        let inner = Rect::new(area.x, area.y, area.width, area.height.saturating_sub(1));
        let [_wordmark, import] =
            Layout::horizontal([Constraint::Percentage(67), Constraint::Percentage(33)])
                .areas(inner);
        import
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
    centered(62, 15, screen)
}

pub(crate) fn model_manager_areas(screen: Rect) -> [Rect; 6] {
    let area = centered(90, 36, screen);
    let inner = Rect::new(
        area.x.saturating_add(1),
        area.y.saturating_add(1),
        area.width.saturating_sub(2),
        area.height.saturating_sub(2),
    );
    let [body, footer] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(5)]).areas(inner);
    let [sidebar, workspace] =
        Layout::horizontal([Constraint::Percentage(24), Constraint::Percentage(76)]).areas(body);
    let [search, results] =
        Layout::vertical([Constraint::Length(2), Constraint::Fill(1)]).areas(workspace);
    let [list, details] =
        Layout::horizontal([Constraint::Percentage(40), Constraint::Percentage(60)]).areas(results);
    [area, sidebar, search, list, details, footer]
}

pub(crate) fn delete_model_confirm_area(screen: Rect) -> Rect {
    centered(52, 9, screen)
}

fn render_chat(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
    previews: &mut [ChatImagePreview],
) {
    let block = panel("Chat", state.focus == FocusPanel::Chat, theme);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let input_height = if inner.height >= 7 { 3 } else { 1 };
    let [body, input] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(input_height)]).areas(inner);
    let evidence_height = if state.chat.answer.is_empty() || state.chat.citations.is_empty() {
        0
    } else {
        body.height.min(10)
    };
    let [answer_block, evidence] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(evidence_height)]).areas(body);
    let marker_height =
        u16::from(!state.chat.answer.is_empty() && !state.chat.citations.is_empty());
    let [answer, markers] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(marker_height)])
            .areas(answer_block);
    let text = if let Some(error) = &state.chat.error {
        Text::from(vec![
            Line::styled("Answer failed", Style::default().fg(theme.red)),
            Line::from(error.as_str()),
        ])
    } else if state.chat.answer.is_empty() {
        let message = if state.chat.request_pending || state.chat.active_run.is_some() {
            format!(
                "{} Searching and composing…",
                spinner(metrics.animation_tick)
            )
        } else {
            "Ask your library. Press Enter to type.".into()
        };
        Text::from(vec![
            Line::styled(
                message,
                Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
            ),
            Line::from(""),
            Line::styled(
                format!(
                    "Evidence: {} · {} citations",
                    state.chat.evidence_mode,
                    state.chat.citations.len()
                ),
                Style::default().fg(theme.muted),
            ),
        ])
    } else {
        highlighted_answer(&state.chat.answer, state.citation_cursor, theme)
    };
    frame.render_widget(
        Paragraph::new(text)
            .wrap(Wrap { trim: false })
            .scroll((state.chat_scroll, 0)),
        answer,
    );
    if evidence_height > 0 {
        let mut marker_spans = vec![Span::styled(" Evidence ", Style::default().fg(theme.muted))];
        for index in 0..state.chat.citations.len().min(9) {
            marker_spans.push(Span::styled(
                format!("[{}]", index + 1),
                Style::default()
                    .fg(if index == state.citation_cursor {
                        theme.background
                    } else {
                        theme.orange
                    })
                    .bg(if index == state.citation_cursor {
                        theme.focus
                    } else {
                        theme.panel
                    })
                    .add_modifier(Modifier::BOLD),
            ));
            marker_spans.push(Span::raw(" "));
        }
        frame.render_widget(Paragraph::new(Line::from(marker_spans)), markers);
        render_chat_evidence(frame, evidence, state, theme, previews);
    }
    render_inline_editor(
        frame,
        input,
        &state.chat.question,
        "Enter ask · Ctrl+E evidence",
        state.focus == FocusPanel::Chat && state.input_mode == InputMode::Text,
        theme,
    );
}

fn render_chat_evidence(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    previews: &mut [ChatImagePreview],
) {
    let preview_count = state
        .chat
        .citations
        .iter()
        .filter(|citation| !citation.pages.is_empty())
        .take(4)
        .count();
    let image_height = if preview_count == 0 {
        0
    } else {
        area.height.saturating_sub(4).min(6)
    };
    let [images, sources] =
        Layout::vertical([Constraint::Length(image_height), Constraint::Fill(1)]).areas(area);
    if image_height > 0 {
        let constraints = (0..preview_count)
            .map(|_| Constraint::Ratio(1, preview_count as u32))
            .collect::<Vec<_>>();
        let cells = Layout::horizontal(constraints).split(images);
        for (index, cell) in cells.iter().enumerate() {
            let page = state
                .chat
                .citations
                .iter()
                .filter_map(|citation| citation.pages.first().copied())
                .nth(index)
                .unwrap_or(0);
            let preview_title = previews.get(index).map_or_else(
                || format!("p.{page}"),
                |preview| truncate(&preview.title, cell.width.saturating_sub(4) as usize),
            );
            let block = Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(if index == state.citation_cursor {
                    theme.focus
                } else {
                    theme.purple
                }))
                .title(format!(" {preview_title} "));
            let inner = block.inner(*cell);
            frame.render_widget(block, *cell);
            if let Some(preview) = previews.get_mut(index) {
                preview.receive_resizes();
                frame.render_stateful_widget(
                    StatefulImage::default(),
                    inner,
                    &mut preview.protocol,
                );
            } else {
                frame.render_widget(
                    Paragraph::new("Rendering preview…")
                        .style(Style::default().fg(theme.muted))
                        .alignment(ratatui::layout::Alignment::Center),
                    inner,
                );
            }
        }
    }
    let lines = state
        .chat
        .citations
        .iter()
        .take(sources.height as usize)
        .enumerate()
        .map(|(index, citation)| {
            let page = citation
                .pages
                .first()
                .copied()
                .map_or_else(|| "p.?".into(), |page| format!("p.{page}"));
            let heading = citation.headings.last().map(String::as_str).unwrap_or("");
            let element = if citation.picture_refs.is_empty() {
                citation
                    .element_types
                    .first()
                    .map(String::as_str)
                    .unwrap_or("")
            } else {
                "Figure"
            };
            let detail = match (heading.is_empty(), element.is_empty()) {
                (false, false) => format!(" · {heading} · {element}"),
                (false, true) => format!(" · {heading}"),
                (true, false) => format!(" · {element}"),
                (true, true) => String::new(),
            };
            Line::from(vec![
                Span::styled(
                    format!(" [{}] ", index + 1),
                    Style::default()
                        .fg(if index == state.citation_cursor {
                            theme.background
                        } else {
                            theme.orange
                        })
                        .bg(if index == state.citation_cursor {
                            theme.focus
                        } else {
                            theme.panel
                        })
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    truncate(
                        &format!(
                            "{} · {page}{detail}",
                            citation.document_title.as_deref().unwrap_or("Source")
                        ),
                        sources.width.saturating_sub(8) as usize,
                    ),
                    Style::default().fg(theme.cyan),
                ),
            ])
        })
        .collect::<Vec<_>>();
    frame.render_widget(
        Paragraph::new(lines).block(
            Block::default()
                .borders(Borders::TOP)
                .title(" Sources ")
                .border_style(Style::default().fg(theme.border)),
        ),
        sources,
    );
}

fn highlighted_answer(answer: &str, selected: usize, theme: &Theme) -> Text<'static> {
    Text::from(
        answer
            .lines()
            .map(|line| {
                let mut spans = Vec::new();
                let mut rest = line;
                while let Some(start) = rest.find('[') {
                    spans.push(Span::raw(rest[..start].to_owned()));
                    let Some(end) = rest[start..].find(']') else {
                        spans.push(Span::raw(rest[start..].to_owned()));
                        rest = "";
                        break;
                    };
                    let marker = &rest[start..=start + end];
                    let index = marker.trim_matches(['[', ']']).parse::<usize>().ok();
                    spans.push(Span::styled(
                        marker.to_owned(),
                        Style::default()
                            .fg(if index == Some(selected + 1) {
                                theme.background
                            } else {
                                theme.orange
                            })
                            .bg(if index == Some(selected + 1) {
                                theme.focus
                            } else {
                                theme.panel
                            })
                            .add_modifier(Modifier::BOLD),
                    ));
                    rest = &rest[start + end + 1..];
                }
                if !rest.is_empty() {
                    spans.push(Span::raw(rest.to_owned()));
                }
                Line::from(spans)
            })
            .collect::<Vec<_>>(),
    )
}

fn render_library(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let workspace = state
        .active_workspace
        .as_ref()
        .and_then(|id| {
            state
                .workspaces
                .iter()
                .find(|workspace| &workspace.id == id)
        })
        .map_or("No library", |workspace| workspace.name.as_str());
    let title = format!("Library · {workspace}");
    let block = panel(&title, state.focus == FocusPanel::Sources, theme);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let [content, actions] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(4)]).areas(inner);
    let jobs = library_jobs(state);
    let documents = library_documents(state);
    let profile = state.active_profile_settings().name;
    let mut items = vec![ListItem::new(Line::from(vec![
        Span::styled(" ◇ ", Style::default().fg(theme.purple)),
        Span::styled(
            format!("{} docs", documents.len()),
            Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!(
                "  {} · {} · {profile}",
                state.library.filter.label(),
                state.library.sort.label()
            ),
            Style::default().fg(theme.muted),
        ),
    ]))];
    for job in &jobs {
        let sources = job
            .payload
            .get("sources")
            .and_then(|sources| sources.as_array());
        let total = sources.map_or(0, Vec::len);
        let current = ((job.progress * total as f64).floor() as usize).min(total.saturating_sub(1));
        let source = sources
            .and_then(|sources| sources.get(current))
            .and_then(|source| source.get("location").or_else(|| source.get("path")))
            .and_then(|path| path.as_str())
            .and_then(|path| std::path::Path::new(path).file_name())
            .and_then(|name| name.to_str())
            .unwrap_or("import");
        let color = if job.status == JobStatus::Failed {
            theme.red
        } else {
            theme.orange
        };
        items.push(ListItem::new(vec![
            Line::from(vec![
                Span::styled(
                    if job.status == JobStatus::Failed {
                        " ! "
                    } else {
                        " ◆ "
                    },
                    Style::default().fg(color),
                ),
                Span::styled(truncate(source, 25), Style::default().fg(theme.text)),
                Span::styled(
                    format!("  {:.0}%", job.progress * 100.0),
                    Style::default().fg(color),
                ),
            ]),
            Line::styled(
                format!(
                    "   {} · {}/{} files",
                    truncate(&job.phase, 24),
                    current.saturating_add(1).min(total),
                    total
                ),
                Style::default().fg(theme.muted),
            ),
        ]));
    }
    for document in &documents {
        let detail = state.library.details.get(&document.id);
        let size = detail.map_or(String::new(), |detail| format_bytes(detail.size_bytes));
        items.push(asset_item(
            "PDF",
            &document.title,
            if size.is_empty() {
                document.status.clone()
            } else {
                size
            },
            theme.cyan,
            theme,
        ));
    }
    if documents.is_empty() && jobs.is_empty() {
        items.push(ListItem::new(Line::styled(
            "   Import PDFs to build this library",
            Style::default().fg(theme.muted),
        )));
    }
    let selected = 1 + state
        .asset_cursor
        .min(jobs.len() + documents.len().saturating_sub(1));
    let mut list_state = ListState::default();
    if !jobs.is_empty() || !documents.is_empty() {
        list_state.select(Some(selected));
    }
    frame.render_stateful_widget(
        List::new(items).highlight_symbol("›").highlight_style(
            Style::default()
                .bg(theme.selection)
                .fg(theme.cyan)
                .add_modifier(Modifier::BOLD),
        ),
        content,
        &mut list_state,
    );
    let controls = if state.library.filtering {
        vec![
            Line::from(vec![
                Span::styled(" Search  ", Style::default().fg(theme.cyan)),
                Span::styled(
                    &state.library.query.value,
                    Style::default().fg(theme.text).add_modifier(Modifier::BOLD),
                ),
            ]),
            Line::styled("Enter apply   Esc close", Style::default().fg(theme.muted)),
            Line::from(""),
            Line::from(""),
        ]
    } else {
        vec![
            shortcut_columns(
                theme,
                &[
                    ("Libraries", 'L', theme.purple),
                    ("New", 'N', theme.green),
                    ("Profile", 'P', theme.orange),
                ],
            ),
            shortcut_columns(
                theme,
                &[
                    ("Search", 'S', theme.cyan),
                    ("Filter", 'F', theme.yellow),
                    ("Sort", 'O', theme.purple),
                ],
            ),
            shortcut_columns(
                theme,
                &[
                    ("View", 'V', theme.cyan),
                    ("Pause", 'U', theme.yellow),
                    ("Retry", 'R', theme.green),
                ],
            ),
            shortcut_columns(
                theme,
                &[
                    ("Info", 'I', theme.cyan),
                    ("Tags", 'T', theme.purple),
                    ("Delete", 'D', theme.red),
                ],
            ),
        ]
    };
    frame.render_widget(Paragraph::new(controls), actions);
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

fn shortcut_columns(theme: &Theme, words: &[(&str, char, Color)]) -> Line<'static> {
    const COLUMN_WIDTH: usize = 12;
    let mut spans = Vec::new();
    for (index, (word, key, color)) in words.iter().enumerate() {
        spans.extend(shortcut_word(theme, word, *key, *color));
        if index + 1 < words.len() {
            spans.push(Span::raw(
                " ".repeat(COLUMN_WIDTH.saturating_sub(word.chars().count())),
            ));
        }
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
                "{} {} {} {}",
                document.title,
                document.source,
                document.status,
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

fn render_compute(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    let memory_percent = if metrics.memory_total == 0 {
        0.0
    } else {
        metrics.memory_used as f64 / metrics.memory_total as f64 * 100.0
    };
    let title = format!(
        "Compute bay · {} active · Enter manage",
        metrics.loaded_models.len()
    );
    let block = panel(
        &title,
        matches!(state.focus, FocusPanel::Hardware | FocusPanel::Models),
        theme,
    );
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let [content, status] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(1)]).areas(inner);
    let [hardware, models] =
        Layout::horizontal([Constraint::Percentage(38), Constraint::Percentage(62)]).areas(content);
    let [hardware_title, hardware_data] =
        Layout::vertical([Constraint::Length(1), Constraint::Fill(1)]).areas(hardware);
    let [models_title, models_data] =
        Layout::vertical([Constraint::Length(1), Constraint::Fill(1)]).areas(models);

    frame.render_widget(
        Paragraph::new(" HARDWARE TELEMETRY").style(
            Style::default()
                .fg(theme.green)
                .add_modifier(Modifier::BOLD),
        ),
        hardware_title,
    );
    frame.render_widget(
        Paragraph::new(" MODEL ROLES").style(
            Style::default()
                .fg(theme.purple)
                .add_modifier(Modifier::BOLD),
        ),
        models_title,
    );

    let hardware_rows = [
        (
            "CPU",
            format!("{:.0}%", metrics.cpu_usage),
            load_color(f64::from(metrics.cpu_usage), theme),
        ),
        (
            "Memory",
            format!(
                "{}/{} · {memory_percent:.0}%",
                human_memory(metrics.memory_used),
                human_memory(metrics.memory_total)
            ),
            load_color(memory_percent, theme),
        ),
        (
            "VRAM*",
            if metrics.vram_total == 0 {
                "shared / unknown".into()
            } else {
                format!(
                    "{}/{} · {:.0}%",
                    human_memory(metrics.vram_used),
                    human_memory(metrics.vram_total),
                    metrics.vram_used as f64 / metrics.vram_total as f64 * 100.0
                )
            },
            load_color(
                if metrics.vram_total == 0 {
                    0.0
                } else {
                    metrics.vram_used as f64 / metrics.vram_total as f64 * 100.0
                },
                theme,
            ),
        ),
        (
            "GPU",
            metrics
                .gpu_name
                .as_deref()
                .unwrap_or("system graphics")
                .to_owned(),
            theme.orange,
        ),
        ("Threads", metrics.cpu_count.to_string(), theme.cyan),
    ];
    let hardware_items = hardware_rows.into_iter().map(|(label, value, color)| {
        ListItem::new(Line::from(vec![
            Span::styled(format!(" {label:<8}"), Style::default().fg(theme.muted)),
            Span::styled(
                truncate(&value, hardware.width.saturating_sub(11) as usize),
                Style::default().fg(color).add_modifier(Modifier::BOLD),
            ),
        ]))
    });
    frame.render_widget(
        List::new(hardware_items).block(
            Block::default()
                .borders(Borders::RIGHT)
                .border_style(Style::default().fg(theme.border)),
        ),
        hardware_data,
    );

    let configured = configured_models(state);
    let model_items = configured.iter().map(|(role, name)| {
        let loaded = metrics
            .loaded_models
            .iter()
            .any(|model| model_matches(&model.name, name));
        ListItem::new(Line::from(vec![
            Span::styled(format!(" {role:<10}"), Style::default().fg(theme.muted)),
            Span::styled(
                if loaded { "● " } else { "○ " },
                Style::default().fg(if loaded { theme.green } else { theme.yellow }),
            ),
            Span::styled(
                truncate(name, models.width.saturating_sub(17) as usize),
                Style::default().fg(theme.text),
            ),
        ]))
    });
    let mut list_state = ListState::default();
    list_state.select(Some(
        state.model_cursor.min(configured.len().saturating_sub(1)),
    ));
    frame.render_stateful_widget(
        List::new(model_items)
            .highlight_symbol("›")
            .highlight_style(
                Style::default()
                    .bg(theme.selection)
                    .add_modifier(Modifier::BOLD),
            ),
        models_data,
        &mut list_state,
    );

    let runtime_status = metrics.loaded_models.first().map_or_else(
        || hardware_recommendation(metrics),
        |model| {
            format!(
                "OLLAMA ACTIVE  {} · model VRAM {} · ctx {} · {}",
                model.name,
                human_memory(model.size_vram),
                model.context_length,
                model.quantization
            )
        },
    );
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                truncate(&runtime_status, status.width.saturating_sub(25) as usize),
                Style::default()
                    .fg(theme.purple)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled("  ·  *desktop + AI", Style::default().fg(theme.muted)),
        ])),
        status,
    );
}

fn render_activity(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    let block = panel("Activity", state.focus == FocusPanel::Activity, theme);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let [jobs, actions] =
        Layout::vertical([Constraint::Fill(1), Constraint::Length(3)]).areas(inner);
    let items = if state.jobs.is_empty() {
        vec![ListItem::new(Line::styled(
            " No jobs",
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
    list_state.select(Some(state.job_cursor.min(items.len().saturating_sub(1))));
    frame.render_stateful_widget(
        List::new(items).highlight_symbol("›").highlight_style(
            Style::default()
                .bg(theme.selection)
                .add_modifier(Modifier::BOLD),
        ),
        jobs,
        &mut list_state,
    );
    let notice = state.notifications.first().map_or_else(
        || Line::styled(" Ready", Style::default().fg(theme.green)),
        |notification| {
            let color = match notification.level {
                NotificationLevel::Info => theme.cyan,
                NotificationLevel::Warning => theme.yellow,
                NotificationLevel::Error => theme.red,
            };
            Line::styled(
                format!(" {}", truncate(&notification.message, 36)),
                Style::default().fg(color),
            )
        },
    );
    let running = state.jobs.values().any(|job| !is_terminal(&job.status));
    let mut activity_controls = shortcut_columns(
        theme,
        &[
            ("Refresh", 'R', theme.cyan),
            ("Stop", 'S', theme.red),
            ("Clear", 'C', theme.yellow),
        ],
    );
    activity_controls.spans.push(Span::styled(
        format!("  {}", spinner(metrics.animation_tick)),
        Style::default().fg(if running { theme.yellow } else { theme.muted }),
    ));
    frame.render_widget(Paragraph::new(vec![notice, activity_controls]), actions);
}

fn render_footer(frame: &mut Frame<'_>, area: Rect, state: &AppState, theme: &Theme) {
    let mode = if state.input_mode == InputMode::Text {
        "TYPE"
    } else {
        "NAV"
    };
    let hints: &[(&str, &str)] = match state.overlay {
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
        None => match state.focus {
            FocusPanel::Chat => &[
                ("Enter", "Ask"),
                ("[ ]", "Citations"),
                ("O", "Open"),
                ("V", "View"),
                ("H", "History"),
                ("X", "Export"),
            ],
            FocusPanel::Import => &[("Enter", "Add"), ("I", "Knowledge"), ("Mouse", "Open")],
            FocusPanel::Sources => &[
                ("↑↓", "Select"),
                ("V", "View"),
                ("S", "Search"),
                ("F", "Filter"),
                ("I", "Info"),
                ("T", "Tags"),
                ("D", "Remove"),
            ],
            FocusPanel::Models | FocusPanel::Hardware => {
                &[("Enter", "Models"), ("F", "Fit"), ("Q", "Quantize")]
            }
            FocusPanel::Activity => &[("↑↓", "Select"), ("Space", "Pause/resume"), ("X", "Cancel")],
            FocusPanel::Navigation => &[("Enter", "Open")],
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

fn render_overlay(
    frame: &mut Frame<'_>,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    match state.overlay {
        Some(Overlay::Palette) => render_palette(frame, state, theme),
        Some(Overlay::Workspaces) => render_libraries(frame, state, theme),
        Some(Overlay::Help) => render_help(frame, theme),
        Some(Overlay::ModelManager) => render_model_manager(frame, state, theme, metrics),
        Some(Overlay::ConfirmModelDelete) => {
            render_model_manager(frame, state, theme, metrics);
            render_delete_model_confirm(frame, state, theme);
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
        Some(Overlay::DocumentTags) => render_document_tags(frame, state, theme),
        None => {}
    }
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
    if let Some(error) = &preflight.error {
        lines.push(Line::styled(error, Style::default().fg(theme.red)));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(vec![
        Span::styled(
            " Enter / Y  Queue import",
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
    let index = state.asset_cursor.checked_sub(library_jobs(state).len())?;
    library_documents(state).get(index).copied()
}

fn render_document_details(frame: &mut Frame<'_>, state: &AppState, theme: &Theme) {
    let area = centered(66, 19, frame.area());
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
            Span::styled("Parser       ", Style::default().fg(theme.muted)),
            Span::raw(format!("{} · Hybrid ≤384", document.parser_id)),
        ]),
        Line::from(vec![
            Span::styled("Size         ", Style::default().fg(theme.muted)),
            Span::raw(detail.map_or("scanning".into(), |item| format_bytes(item.size_bytes))),
        ]),
        Line::from(vec![
            Span::styled("Chunks       ", Style::default().fg(theme.muted)),
            Span::raw(
                detail
                    .and_then(|item| item.chunks)
                    .map_or("provided by Haiku".into(), |chunks| chunks.to_string()),
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

fn render_model_manager(
    frame: &mut Frame<'_>,
    state: &AppState,
    theme: &Theme,
    metrics: &RuntimeMetrics,
) {
    let [area, sidebar, search, list_area, details_area, footer] =
        model_manager_areas(frame.area());
    frame.render_widget(Clear, area);
    let block = panel("Model foundry · three hardware-matched stacks", true, theme);
    frame.render_widget(block, area);
    let mut navigation = vec![Line::styled(
        " PROVIDERS",
        Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
    )];
    navigation.extend(
        [
            ModelSource::Installed,
            ModelSource::Ollama,
            ModelSource::HuggingFace,
        ]
        .into_iter()
        .map(|source| {
            let active = source == state.model_manager.source;
            Line::styled(
                format!(" {} {}", if active { "›" } else { " " }, source.label()),
                if active {
                    Style::default()
                        .fg(theme.background)
                        .bg(theme.cyan)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(theme.muted)
                },
            )
        }),
    );
    navigation.push(Line::from(""));
    navigation.push(Line::styled(
        " ROLES",
        Style::default()
            .fg(theme.purple)
            .add_modifier(Modifier::BOLD),
    ));
    navigation.extend(
        [
            ModelCategory::Chat,
            ModelCategory::Vl,
            ModelCategory::Embedding,
            ModelCategory::Rerank,
        ]
        .into_iter()
        .map(|category| {
            let active = category == state.model_manager.category;
            Line::styled(
                format!(" {} {}", if active { "›" } else { " " }, category.label()),
                if active {
                    Style::default()
                        .fg(theme.background)
                        .bg(theme.purple)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(theme.muted)
                },
            )
        }),
    );
    navigation.push(Line::from(""));
    navigation.push(Line::styled(
        " RECOMMENDED STACKS",
        Style::default()
            .fg(theme.orange)
            .add_modifier(Modifier::BOLD),
    ));
    navigation.extend(
        state
            .model_manager
            .packages
            .iter()
            .enumerate()
            .map(|(index, package)| {
                let active = index == state.model_manager.package_cursor;
                Line::styled(
                    format!(
                        " {} #{} {}",
                        if active { "›" } else { " " },
                        package.recommended_rank,
                        truncate(&package.name, sidebar.width.saturating_sub(8) as usize)
                    ),
                    if active {
                        Style::default()
                            .fg(theme.background)
                            .bg(theme.orange)
                            .add_modifier(Modifier::BOLD)
                    } else {
                        Style::default().fg(theme.muted)
                    },
                )
            }),
    );
    navigation.extend([
        Line::from(""),
        Line::styled(" Tab  provider", Style::default().fg(theme.muted)),
        Line::styled(" ⇧←→ role · 1–3 stack", Style::default().fg(theme.muted)),
    ]);
    frame.render_widget(
        Paragraph::new(navigation).block(
            Block::default()
                .borders(Borders::RIGHT)
                .border_style(Style::default().fg(theme.border)),
        ),
        sidebar,
    );

    let search_style = if state.model_manager.searching {
        Style::default()
            .fg(theme.yellow)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(theme.muted)
    };
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(" / Search  ", search_style),
            Span::styled(
                if state.model_manager.query.value.is_empty() {
                    "type a model or repository"
                } else {
                    state.model_manager.query.value.as_str()
                },
                Style::default().fg(if state.model_manager.query.value.is_empty() {
                    theme.muted
                } else {
                    theme.text
                }),
            ),
        ]))
        .block(
            Block::default()
                .borders(Borders::BOTTOM)
                .border_style(Style::default().fg(theme.border)),
        ),
        search,
    );
    if state.model_manager.searching {
        let x = search.x
            + 11
            + UnicodeWidthStr::width(
                &state.model_manager.query.value[..state
                    .model_manager
                    .query
                    .cursor
                    .min(state.model_manager.query.value.len())],
            ) as u16;
        frame.set_cursor_position((x.min(search.right().saturating_sub(1)), search.y));
    }
    let entries = state
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
                    entry
                        .recommended_rank
                        .map_or_else(|| "    ".into(), |rank| format!("#{rank} ")),
                    Style::default()
                        .fg(theme.orange)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    if loaded {
                        " ● "
                    } else if entry.installed {
                        " ✓ "
                    } else {
                        " ○ "
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
                    truncate(&entry.id, list_area.width.saturating_sub(9) as usize),
                    Style::default().fg(theme.text),
                ),
            ]))
        })
        .collect::<Vec<_>>();
    let mut list_state = ListState::default();
    if !entries.is_empty() {
        list_state.select(Some(
            state
                .model_manager
                .cursor
                .min(entries.len().saturating_sub(1)),
        ));
    }
    frame.render_stateful_widget(
        List::new(entries)
            .highlight_symbol("›")
            .highlight_style(
                Style::default()
                    .bg(theme.selection)
                    .fg(theme.cyan)
                    .add_modifier(Modifier::BOLD),
            )
            .block(
                Block::default()
                    .borders(Borders::RIGHT)
                    .border_style(Style::default().fg(theme.border)),
            ),
        list_area,
        &mut list_state,
    );

    let model_text = state
        .model_manager
        .entries
        .get(state.model_manager.cursor)
        .map_or_else(
            || {
                Text::from(vec![
                    Line::styled(
                        if state.model_manager.busy {
                            "Loading model catalog…"
                        } else {
                            "No matching models"
                        },
                        Style::default().fg(theme.muted),
                    ),
                    Line::from(""),
                    Line::from("Use / to search or Tab to change source."),
                ])
            },
            |entry| model_details(entry, state, metrics, theme),
        );
    let mut detail_lines = state
        .model_manager
        .packages
        .get(state.model_manager.package_cursor)
        .map_or_else(Vec::new, |package| package_details(package, theme));
    if !detail_lines.is_empty() {
        detail_lines.push(Line::styled(
            "──────────────── selected model",
            Style::default().fg(theme.border),
        ));
    }
    detail_lines.extend(model_text.lines);
    frame.render_widget(
        Paragraph::new(Text::from(detail_lines))
            .wrap(Wrap { trim: false })
            .block(Block::default().padding(ratatui::widgets::Padding::horizontal(2))),
        details_area,
    );

    let percent = if state.model_manager.transfer_total == 0 {
        None
    } else {
        Some(
            state.model_manager.transfer_completed as f64
                / state.model_manager.transfer_total as f64
                * 100.0,
        )
    };
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(vec![
                Span::styled(
                    "F",
                    Style::default()
                        .fg(theme.green)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    format!("it {}  ", state.model_manager.profile.label()),
                    Style::default().fg(theme.text),
                ),
                Span::styled(
                    "Q",
                    Style::default()
                        .fg(theme.purple)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    format!("uant {}  ", state.model_manager.quantization.label()),
                    Style::default().fg(theme.text),
                ),
                Span::styled(
                    "C",
                    Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    format!("ontext {}K  ", state.model_manager.context_tokens / 1024),
                    Style::default().fg(theme.text),
                ),
                Span::styled(
                    "P",
                    Style::default()
                        .fg(theme.orange)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::styled(
                    format!("urge {}", state.model_manager.memory_policy.label()),
                    Style::default().fg(theme.text),
                ),
            ]),
            Line::from(vec![
                Span::styled(
                    "D",
                    Style::default()
                        .fg(theme.green)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw("ownload   "),
                Span::styled(
                    "L",
                    Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
                ),
                Span::raw("oad temporarily   "),
                Span::styled(
                    "U",
                    Style::default().fg(theme.red).add_modifier(Modifier::BOLD),
                ),
                Span::raw("nload   "),
                Span::styled(
                    "R",
                    Style::default()
                        .fg(theme.yellow)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw("efresh"),
                Span::raw("   "),
                Span::styled(
                    "A",
                    Style::default()
                        .fg(theme.orange)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw("pply stack"),
                Span::raw("   "),
                Span::styled(
                    "X",
                    Style::default().fg(theme.red).add_modifier(Modifier::BOLD),
                ),
                Span::raw(" Delete"),
            ]),
            Line::styled(
                if let Some(percent) = percent {
                    format!("{} · {percent:.0}%", state.model_manager.transfer_status)
                } else if state.model_manager.transfer_status.is_empty() {
                    "Ready".into()
                } else {
                    state.model_manager.transfer_status.clone()
                },
                Style::default().fg(if state.model_manager.busy {
                    theme.yellow
                } else {
                    theme.green
                }),
            ),
            Line::styled(
                format!(
                    "Shift+←/→ role · 1–3 stack · Top 3 pinned · {}{}",
                    hardware_recommendation(metrics),
                    if state.model_manager.truncated {
                        " · Hub scan capped; search scans matching repos"
                    } else {
                        ""
                    }
                ),
                Style::default()
                    .fg(theme.orange)
                    .add_modifier(Modifier::BOLD),
            ),
        ])
        .block(
            Block::default()
                .borders(Borders::TOP)
                .border_style(Style::default().fg(theme.border)),
        ),
        footer,
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
    let area = centered(68, 22, frame.area());
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled(
                "Movement",
                Style::default().fg(theme.cyan).add_modifier(Modifier::BOLD),
            ),
            Line::from("Tab / Shift+Tab   next / previous panel"),
            Line::from("Arrow keys         always act on the focused panel"),
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
            Line::from("Ctrl+C Chat         Ctrl+S Library      Ctrl+H Compute"),
            Line::from("Ctrl+M Models       Ctrl+A Activity     Ctrl+T Theme"),
            Line::from("Ctrl+L Libraries    Ctrl+P Palette      Ctrl+Q Quit"),
            Line::from("Ctrl+I Knowledge    N New library      Ctrl+E Evidence"),
            Line::from("Ctrl+R Refresh      Ctrl+X Stop         Ctrl+D Clear"),
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

fn shortcut_panel(title: &str, key: char, focused: bool, theme: &Theme) -> Block<'static> {
    let accent = theme.orange;
    let base = if focused {
        Style::default()
            .fg(theme.background)
            .bg(accent)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(accent).add_modifier(Modifier::BOLD)
    };
    let key_style = base
        .fg(if focused { theme.cyan } else { theme.green })
        .add_modifier(Modifier::UNDERLINED);
    let lower = title.to_ascii_lowercase();
    let needle = key.to_ascii_lowercase().to_string();
    let position = lower.find(&needle).unwrap_or_default();
    let end = position + key.len_utf8();
    let marker = if focused { " ◆ " } else { " " };
    let title = Line::from(vec![
        Span::styled(format!("{marker}{}", &title[..position]), base),
        Span::styled(title[position..end].to_owned(), key_style),
        Span::styled(format!("{} ", &title[end..]), base),
    ]);
    Block::default()
        .borders(Borders::ALL)
        .border_type(if focused {
            BorderType::Thick
        } else {
            BorderType::Plain
        })
        .title(title)
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

fn asset_item(
    icon: impl Into<String>,
    label: impl Into<String>,
    detail: impl Into<String>,
    color: Color,
    theme: &Theme,
) -> ListItem<'static> {
    let icon = icon.into();
    let label = label.into();
    let detail = detail.into();
    ListItem::new(Line::from(vec![
        Span::styled(
            format!(" {icon} "),
            Style::default().fg(color).add_modifier(Modifier::BOLD),
        ),
        Span::styled(truncate(&label, 24), Style::default().fg(theme.text)),
        Span::styled(
            format!("  {}", truncate(&detail, 12)),
            Style::default().fg(theme.muted),
        ),
    ]))
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

fn configured_models(state: &AppState) -> Vec<(String, String)> {
    let content = state
        .config
        .as_ref()
        .map_or("", |config| config.content.as_str());
    let chat = config_model(content, "qa").unwrap_or_else(|| "not configured".into());
    let vision = if config_flag(content, "qa", "vision") {
        chat.clone()
    } else {
        config_model_after(content, "picture_description")
            .unwrap_or_else(|| "not configured".into())
    };
    vec![
        ("Chat".into(), chat),
        ("VL".into(), vision),
        (
            "Embedding".into(),
            config_model(content, "embeddings").unwrap_or_else(|| "not configured".into()),
        ),
        (
            "Rerank".into(),
            config_model(content, "reranking").unwrap_or_else(|| "not configured".into()),
        ),
    ]
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

fn load_color(percent: f64, theme: &Theme) -> Color {
    if percent >= 90.0 {
        theme.red
    } else if percent >= 70.0 {
        theme.yellow
    } else {
        theme.green
    }
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

    #[test]
    fn dashboard_contains_four_distinct_work_areas_and_header_import() {
        for (width, height) in [(160, 42), (128, 36), (90, 24), (72, 20)] {
            let content = rendered(width, height, &AppState::default(), Theme::default());
            for title in [
                "Chat",
                "Library",
                "Compute bay",
                "Activity",
                "Index new PDFs",
            ] {
                assert!(
                    content.contains(title),
                    "missing {title} at {width}x{height}"
                );
            }
            assert!(!content.contains("Nav"));
        }
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
    fn focused_panel_has_a_high_contrast_title() {
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
                .any(|cell| cell.bg == panel_accent("Chat", &theme))
        );
    }

    #[test]
    fn mirrored_oracle_wordmark_pulses_only_with_ollama_activity() {
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
        assert!(idle.contains("OF DÆDALUS"));
        assert!(idle.contains("OFFLINE RETRIEVAL-AUGMENTED COMMAND-LINE ENVIRONMENT"));
        assert!(idle.contains("Index new PDFs"));
        assert!(!idle.contains("OLLAMA IDLE"));
        assert_ne!(idle, active);
    }

    #[test]
    fn dashboard_uses_a_compact_aligned_lower_row() {
        let [chat, library, compute, activity] = dashboard_areas(Rect::new(0, 0, 160, 36));

        assert!(compute.height < chat.height);
        assert_eq!(chat.x, compute.x);
        assert_eq!(library.x, activity.x);
        assert_eq!(chat.width, compute.width);
        assert_eq!(library.width, activity.width);
        assert_eq!(chat.y, library.y);
        assert_eq!(chat.height, library.height);
    }

    #[test]
    fn add_knowledge_is_aligned_with_the_library_column() {
        let screen = Rect::new(0, 0, 160, 42);
        let [header, body, _footer] = screen_areas(screen);
        let import = header_import_area(header);
        let [_chat, library, _compute, activity] = dashboard_areas(body);

        assert_eq!(import.x, library.x);
        assert_eq!(import.width, library.width);
        assert_eq!(library.x, activity.x);
        assert_eq!(library.width, activity.width);
    }

    #[test]
    fn dashboard_geometry_snapshot() {
        let screen = Rect::new(0, 0, 160, 42);
        let [header, body, footer] = screen_areas(screen);
        let import = header_import_area(header);
        let [chat, library, compute, activity] = dashboard_areas(body);
        insta::assert_debug_snapshot!(
            (header, import, chat, library, compute, activity, footer),
            @r###"
        (
            Rect {
                x: 0,
                y: 0,
                width: 160,
                height: 5,
            },
            Rect {
                x: 107,
                y: 0,
                width: 53,
                height: 4,
            },
            Rect {
                x: 0,
                y: 5,
                width: 107,
                height: 24,
            },
            Rect {
                x: 107,
                y: 5,
                width: 53,
                height: 24,
            },
            Rect {
                x: 0,
                y: 29,
                width: 107,
                height: 12,
            },
            Rect {
                x: 107,
                y: 29,
                width: 53,
                height: 12,
            },
            Rect {
                x: 0,
                y: 41,
                width: 160,
                height: 1,
            },
        )
        "###
        );
    }

    #[test]
    fn theme_action_cycles_at_runtime() {
        let mut state = AppState::default();
        update(&mut state, Action::CycleTheme);
        assert_eq!(Theme::at(state.theme_index).name, "Nord Harbor");
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
    fn model_manager_renders_metadata_and_memory_controls() {
        let mut state = AppState {
            overlay: Some(Overlay::ModelManager),
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
        let content = rendered(140, 40, &state, Theme::default());
        for expected in [
            "Model foundry",
            "Qwen Unified",
            "Qwen retrieval family",
            "owner/tiny-GGUF",
            "A small multilingual model",
            "Download",
            "Purge",
        ] {
            assert!(content.contains(expected), "missing {expected}");
        }
    }
}
