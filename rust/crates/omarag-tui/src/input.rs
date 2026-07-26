use crate::{
    centered, confirm_import_area, dashboard_areas, delete_model_confirm_area, file_browser_areas,
    header_import_area, model_manager_areas, screen_areas,
};
use crossterm::{
    event::{
        Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent,
        MouseEventKind,
    },
    terminal,
};
use nucleo_matcher::{
    Config, Matcher, Utf32Str,
    pattern::{CaseMatching, Normalization, Pattern},
};
use omarag_app::{
    Action, AppState, ChatSession, CustomLibraryProfile, EditorState, FocusPanel, HardwareProfile,
    ImportPreflight, InputMode, LibraryFilter, ModelCategory, ModelMemoryPolicy, ModelQuantization,
    ModelSource, Notification, NotificationLevel, Overlay, Route, UndoAction, WorkspaceProfile,
    update,
};
use omarag_domain::{
    CreateSource, CreateWorkspace, DocumentSummary, EvidenceMode, IngestRequest, JobId, JobStatus,
    RunId, SearchRequest, UpdateConfig, WorkspaceId,
};
use ratatui::layout::Rect;
use std::path::{MAIN_SEPARATOR, Path, PathBuf};
use unicode_width::UnicodeWidthChar;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobCommand {
    Pause,
    Resume,
    Cancel,
}

#[derive(Debug, Clone, PartialEq)]
pub enum UiCommand {
    OpenWorkspace(WorkspaceId),
    CreateWorkspace(CreateWorkspace),
    DeleteLibrary {
        id: WorkspaceId,
        physical: bool,
    },
    StartRun {
        workspace: WorkspaceId,
        question: String,
        evidence_mode: EvidenceMode,
    },
    CancelRun(RunId),
    Search {
        workspace: WorkspaceId,
        request: SearchRequest,
    },
    Ingest {
        workspace: WorkspaceId,
        request: IngestRequest,
    },
    Job {
        id: JobId,
        command: JobCommand,
    },
    RefreshJobs,
    RefreshWorkspaceFeatures(WorkspaceId),
    CreateBackup(WorkspaceId),
    CreateSource {
        workspace: WorkspaceId,
        request: CreateSource,
    },
    SaveConfig {
        workspace: WorkspaceId,
        request: UpdateConfig,
        etag: String,
    },
    RefreshModelCatalog {
        source: ModelSource,
        category: ModelCategory,
        query: String,
        quantization: String,
        context_tokens: u32,
        profile: HardwareProfile,
    },
    PullModel {
        model: String,
    },
    PullPackage {
        name: String,
        models: Vec<String>,
    },
    PreloadModel {
        model: String,
        context_tokens: u32,
        keep_alive: String,
    },
    UnloadModel {
        model: String,
    },
    DeleteModel {
        model: String,
        confirm: String,
    },
    OpenPdf {
        path: String,
        page: Option<u32>,
    },
    OpenPageImage {
        path: String,
        page: u32,
        primary_anchors: Vec<omarag_domain::CitationAnchor>,
        context_anchors: Vec<omarag_domain::CitationAnchor>,
    },
    AnalyzeImport {
        selected: Vec<String>,
        existing: Vec<String>,
    },
    DeleteDocument {
        workspace: WorkspaceId,
        document: DocumentSummary,
    },
    RestoreDocument {
        workspace: WorkspaceId,
        document: DocumentSummary,
    },
    ExportChat {
        workspace: String,
        session: ChatSession,
    },
    CopyText(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PaletteCommand {
    Chat,
    Library,
    Sources,
    Jobs,
    Search,
    Quality,
    Backups,
    Settings,
    System,
    SwitchWorkspace,
    ToggleLevel,
    RefreshJobs,
    RefreshWorkspace,
    CreateBackup,
    CancelRun,
    Help,
}

impl PaletteCommand {
    pub const ALL: [Self; 15] = [
        Self::Chat,
        Self::Library,
        Self::Jobs,
        Self::Search,
        Self::Quality,
        Self::Backups,
        Self::Settings,
        Self::System,
        Self::SwitchWorkspace,
        Self::ToggleLevel,
        Self::RefreshJobs,
        Self::RefreshWorkspace,
        Self::CreateBackup,
        Self::CancelRun,
        Self::Help,
    ];

    pub const fn label(self) -> &'static str {
        match self {
            Self::Chat => "Focus chat",
            Self::Library => "Open library tools",
            Self::Sources => "Open source tools",
            Self::Jobs => "Focus activity",
            Self::Search => "Open search tools",
            Self::Quality => "Show quality report",
            Self::Backups => "Show backups",
            Self::Settings => "Open settings",
            Self::System => "Show system status",
            Self::SwitchWorkspace => "Switch library",
            Self::ToggleLevel => "Toggle simple/advanced",
            Self::RefreshJobs => "Refresh jobs",
            Self::RefreshWorkspace => "Refresh library data",
            Self::CreateBackup => "Create backup",
            Self::CancelRun => "Stop active answer",
            Self::Help => "Show keyboard help",
        }
    }

    fn allowed(self, state: &AppState) -> bool {
        match self {
            Self::CancelRun => state.chat.active_run.is_some(),
            Self::CreateBackup | Self::RefreshWorkspace => state.active_workspace.is_some(),
            _ => true,
        }
    }
}

pub fn filtered_palette_commands(state: &AppState) -> Vec<PaletteCommand> {
    let query = state.palette.query.value.trim();
    if query.is_empty() {
        return PaletteCommand::ALL
            .into_iter()
            .filter(|command| command.allowed(state))
            .collect();
    }
    let pattern = Pattern::parse(query, CaseMatching::Ignore, Normalization::Smart);
    let mut matcher = Matcher::new(Config::DEFAULT.match_paths());
    let allowed = PaletteCommand::ALL
        .into_iter()
        .filter(|command| command.allowed(state))
        .collect::<Vec<_>>();
    pattern
        .match_list(allowed.iter().map(|command| command.label()), &mut matcher)
        .into_iter()
        .filter_map(|(label, _score)| {
            allowed
                .iter()
                .find(|command| command.label() == label)
                .copied()
        })
        .collect()
}

pub fn fuzzy_score(candidate: &str, query: &str) -> Option<u32> {
    if query.trim().is_empty() {
        return Some(0);
    }
    let pattern = Pattern::parse(query, CaseMatching::Ignore, Normalization::Smart);
    let mut matcher = Matcher::new(Config::DEFAULT.match_paths());
    let mut buffer = Vec::new();
    pattern.score(Utf32Str::new(candidate, &mut buffer), &mut matcher)
}

pub fn handle_event(state: &mut AppState, event: Event) -> Option<UiCommand> {
    match event {
        Event::Key(key) if matches!(key.kind, KeyEventKind::Press | KeyEventKind::Repeat) => {
            handle_key(state, key)
        }
        Event::Paste(text) => {
            handle_paste(state, &text);
            None
        }
        Event::Mouse(mouse) => {
            let (width, height) = terminal::size().unwrap_or((80, 24));
            handle_mouse(state, mouse, Rect::new(0, 0, width, height))
        }
        Event::FocusLost | Event::FocusGained | Event::Resize(_, _) => None,
        Event::Key(_) => None,
    }
}

fn handle_mouse(state: &mut AppState, mouse: MouseEvent, screen: Rect) -> Option<UiCommand> {
    match mouse.kind {
        MouseEventKind::Down(MouseButton::Right) => {
            if state.overlay == Some(Overlay::ConfirmModelDelete) {
                cancel_model_delete(state);
            } else if state.overlay == Some(Overlay::ConfirmImport) {
                state.overlay = Some(Overlay::FileBrowser);
            } else if state.overlay.is_some() {
                update(state, Action::CloseOverlay);
            } else {
                update(state, Action::SetInputMode(InputMode::Nav));
            }
            None
        }
        MouseEventKind::Down(MouseButton::Middle) => {
            update(state, Action::CycleTheme);
            None
        }
        MouseEventKind::ScrollLeft => {
            if state.overlay.is_none() {
                update(state, Action::FocusPrevious);
            }
            None
        }
        MouseEventKind::ScrollRight => {
            if state.overlay.is_none() {
                update(state, Action::FocusNext);
            }
            None
        }
        MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
            handle_mouse_scroll(state, mouse, screen);
            None
        }
        MouseEventKind::Down(MouseButton::Left) => handle_mouse_primary(state, mouse, screen, true),
        MouseEventKind::Drag(MouseButton::Left) => {
            handle_mouse_primary(state, mouse, screen, false)
        }
        MouseEventKind::Up(_) | MouseEventKind::Moved | MouseEventKind::Drag(_) => None,
    }
}

fn handle_mouse_primary(
    state: &mut AppState,
    mouse: MouseEvent,
    screen: Rect,
    activate: bool,
) -> Option<UiCommand> {
    if let Some(overlay) = state.overlay {
        return handle_overlay_mouse(state, overlay, mouse, screen, activate);
    }

    let [header, body, footer] = screen_areas(screen);
    if contains(header, &mouse) {
        if contains(header_import_area(header), &mouse) {
            update(state, Action::SetFocus(FocusPanel::Import));
            update(state, Action::SetInputMode(InputMode::Nav));
            if activate {
                open_file_browser(state);
            }
        }
        return None;
    }
    if contains(footer, &mouse) {
        if activate {
            update(state, Action::OpenOverlay(Overlay::Help));
        }
        return None;
    }

    let [chat, library, compute, activity] = dashboard_areas(body);
    if contains(chat, &mouse) {
        update(state, Action::Navigate(Route::Chat));
        let inner = bordered_inner(chat);
        let input_height = if inner.height >= 7 { 3 } else { 1 };
        let input_y = inner.bottom().saturating_sub(input_height);
        if mouse.row >= input_y {
            update(state, Action::SetInputMode(InputMode::Text));
            set_editor_cursor_from_column(
                &mut state.chat.question,
                mouse.column.saturating_sub(inner.x) as usize,
            );
        } else if !state.chat.answer.is_empty() && !state.chat.citations.is_empty() {
            let body_height = inner.height.saturating_sub(input_height);
            let evidence_height = body_height.min(10);
            let evidence_y = input_y.saturating_sub(evidence_height);
            let image_height = evidence_height.saturating_sub(4).min(6);
            let targets = citation_targets(state);
            if mouse.row == evidence_y.saturating_sub(1) {
                let marker_start = inner.x.saturating_add(10);
                let index = mouse.column.saturating_sub(marker_start) as usize / 4;
                if index < state.chat.citations.len() {
                    state.citation_cursor = index;
                    state.gallery_cursor = index.min(3);
                    if activate
                        && let Some(citation) = state.chat.citations.get(index)
                        && let Some((path, page)) = citation_pdf_target(state, citation)
                    {
                        return Some(UiCommand::OpenPdf {
                            path,
                            page: Some(page),
                        });
                    }
                }
            } else if mouse.row >= evidence_y
                && mouse.row < evidence_y.saturating_add(image_height)
                && !targets.is_empty()
            {
                let relative = mouse.column.saturating_sub(inner.x) as usize;
                let index = relative.saturating_mul(targets.len()) / inner.width.max(1) as usize;
                let index = index.min(targets.len() - 1);
                state.citation_cursor = index;
                state.gallery_cursor = index;
                if activate && let Some((path, page)) = targets.get(index) {
                    let citation = &state.chat.citations[index];
                    return Some(UiCommand::OpenPageImage {
                        path: path.clone(),
                        page: *page,
                        primary_anchors: citation.primary_anchors.clone(),
                        context_anchors: citation.context_anchors.clone(),
                    });
                }
            } else if mouse.row > evidence_y.saturating_add(image_height) {
                let index = mouse
                    .row
                    .saturating_sub(evidence_y.saturating_add(image_height + 1))
                    as usize;
                if activate
                    && let Some(citation) = state.chat.citations.get(index)
                    && let Some((path, page)) = citation_pdf_target(state, citation)
                {
                    state.citation_cursor = index;
                    return Some(UiCommand::OpenPdf {
                        path,
                        page: Some(page),
                    });
                }
            }
        }
        return None;
    }
    if contains(library, &mouse) {
        return handle_library_mouse(state, mouse, library, activate);
    }
    if contains(compute, &mouse) {
        let was_focused = matches!(state.focus, FocusPanel::Hardware | FocusPanel::Models);
        update(state, Action::SetFocus(FocusPanel::Models));
        update(state, Action::SetInputMode(InputMode::Nav));
        let inner = bordered_inner(compute);
        let content_height = inner.height.saturating_sub(1);
        let models_x = inner.x + inner.width.saturating_mul(38) / 100;
        if mouse.column >= models_x && mouse.row > inner.y {
            state.model_cursor = mouse.row.saturating_sub(inner.y + 1).min(3) as usize;
        }
        if activate
            && was_focused
            && mouse.column >= models_x
            && mouse.row < inner.y + content_height
        {
            return open_model_manager(state);
        }
        return None;
    }
    if contains(activity, &mouse) {
        return handle_activity_mouse(state, mouse, activity, activate);
    }
    None
}

fn citation_pdf_target(
    state: &AppState,
    citation: &omarag_domain::Citation,
) -> Option<(String, u32)> {
    let page = citation.pages.first().copied()?;
    if let Some(source) = citation.source_uri.as_deref() {
        if let Ok(uri) = url::Url::parse(source)
            && uri.scheme() == "file"
            && let Ok(path) = uri.to_file_path()
        {
            return Some((path.to_string_lossy().into_owned(), page));
        }
        if !source.contains("://") {
            return Some((source.to_owned(), page));
        }
    }
    let document = citation
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
        })?;
    Some((document.source.clone(), page))
}

fn citation_targets(state: &AppState) -> Vec<(String, u32)> {
    let mut targets = Vec::new();
    for citation in &state.chat.citations {
        let Some(target) = citation_pdf_target(state, citation) else {
            continue;
        };
        if !targets.contains(&target) {
            targets.push(target);
        }
        if targets.len() == 4 {
            break;
        }
    }
    targets
}

fn handle_library_mouse(
    state: &mut AppState,
    mouse: MouseEvent,
    area: Rect,
    activate: bool,
) -> Option<UiCommand> {
    let inner = bordered_inner(area);
    update(state, Action::SetFocus(FocusPanel::Sources));
    update(state, Action::SetInputMode(InputMode::Nav));
    let actions_y = inner.bottom().saturating_sub(4);
    if mouse.row >= actions_y && activate {
        let relative = mouse.column.saturating_sub(inner.x);
        match mouse.row.saturating_sub(actions_y) {
            0 => match relative.saturating_mul(3) / inner.width.max(1) {
                0 => {
                    state.creating_workspace = false;
                    state.overlay = Some(Overlay::Workspaces);
                }
                1 => start_workspace_creation(state),
                _ => {
                    state.profile_cursor = state.active_profile_index();
                    state.overlay = Some(Overlay::WorkspaceProfile);
                }
            },
            1 => match relative.saturating_mul(3) / inner.width.max(1) {
                0 => {
                    state.library.filtering = true;
                    state.input_mode = InputMode::Text;
                }
                1 => {
                    state.library.filter = state.library.filter.next();
                    state.asset_cursor = 0;
                }
                _ => {
                    state.library.sort = state.library.sort.next();
                    state.asset_cursor = 0;
                }
            },
            2 => {
                return match relative.saturating_mul(3) / inner.width.max(1) {
                    0 => activate_selection(state),
                    1 => toggle_selected_library_job(state),
                    _ => retry_selected_library_job(state),
                };
            }
            _ => {
                return match relative.saturating_mul(3) / inner.width.max(1) {
                    0 => {
                        if selected_library_document(state).is_some() {
                            state.overlay = Some(Overlay::DocumentDetails);
                        }
                        None
                    }
                    1 => {
                        if let Some(document) = selected_library_document(state) {
                            let tags = state
                                .document_tags
                                .get(&document.id)
                                .cloned()
                                .unwrap_or_default()
                                .join(", ");
                            state.tag_editor.set(tags);
                            state.overlay = Some(Overlay::DocumentTags);
                        }
                        None
                    }
                    _ => {
                        if selected_library_document(state).is_some() {
                            state.overlay = Some(Overlay::ConfirmDocumentDelete);
                        } else {
                            hide_selected_library_job(state);
                        }
                        None
                    }
                };
            }
        }
        return None;
    }
    let jobs = visible_library_jobs(state);
    let active_jobs = jobs.len();
    let jobs_end = inner.y.saturating_add(1 + active_jobs as u16 * 2);
    if mouse.row >= inner.y.saturating_add(1) && mouse.row < jobs_end {
        state.asset_cursor = (mouse.row.saturating_sub(inner.y + 1) / 2) as usize;
        return if activate {
            toggle_selected_library_job(state)
        } else {
            None
        };
    }
    let document_start = jobs_end;
    if mouse.row >= document_start {
        let index = mouse.row.saturating_sub(document_start) as usize;
        let documents = visible_document_indices(state);
        if let Some(document_index) = documents.get(index).copied() {
            state.asset_cursor = active_jobs + index;
            state.document_cursor = document_index;
            if activate {
                let document = &state.documents[document_index];
                return Some(UiCommand::OpenPdf {
                    path: document.source.clone(),
                    page: None,
                });
            }
        }
    }
    None
}

fn handle_activity_mouse(
    state: &mut AppState,
    mouse: MouseEvent,
    area: Rect,
    activate: bool,
) -> Option<UiCommand> {
    update(state, Action::SetFocus(FocusPanel::Activity));
    update(state, Action::SetInputMode(InputMode::Nav));
    let inner = bordered_inner(area);
    let actions_y = inner.bottom().saturating_sub(3);
    if mouse.row < actions_y {
        state.job_cursor = (mouse.row.saturating_sub(inner.y) as usize / 2)
            .min(state.jobs.len().saturating_sub(1));
        return None;
    }
    if !activate {
        return None;
    }
    let relative_x = mouse.column.saturating_sub(inner.x);
    let action = relative_x.saturating_mul(3) / inner.width.max(1);
    match action {
        0 => Some(UiCommand::RefreshJobs),
        1 => cancel_active(state),
        _ => {
            update(state, Action::NotificationDismissed);
            None
        }
    }
}

fn handle_overlay_mouse(
    state: &mut AppState,
    overlay: Overlay,
    mouse: MouseEvent,
    screen: Rect,
    activate: bool,
) -> Option<UiCommand> {
    match overlay {
        Overlay::Help => {
            if activate {
                update(state, Action::CloseOverlay);
            }
            None
        }
        Overlay::Workspaces => {
            let height = if state.creating_workspace {
                16
            } else {
                (state.workspaces.len() as u16 + 7).clamp(10, 21)
            };
            let area = centered(58, height, screen);
            if !contains(area, &mouse) {
                return None;
            }
            if state.creating_workspace {
                if mouse.row == area.y.saturating_add(3) {
                    set_editor_cursor_from_column(
                        &mut state.workspace_name,
                        mouse.column.saturating_sub(area.x.saturating_add(3)) as usize,
                    );
                } else if activate && mouse.row == area.y.saturating_add(7) {
                    state.profile_cursor =
                        (state.profile_cursor + 1) % state.profile_count().max(1);
                } else if activate && mouse.row == area.bottom().saturating_sub(2) {
                    if mouse.column < area.x.saturating_add(area.width / 2) {
                        return create_workspace_command(state);
                    }
                    cancel_workspace_creation(state);
                }
                return None;
            }
            let index = mouse.row.saturating_sub(area.y.saturating_add(1)) as usize;
            if index < state.workspaces.len() {
                state.workspace_cursor = index;
                if activate && let Some(id) = selected_workspace(state) {
                    if let Some(previous) = state
                        .active_workspace
                        .clone()
                        .filter(|previous| previous != &id)
                    {
                        state.undo = Some(UndoAction::WorkspaceChanged(previous));
                    }
                    update(state, Action::WorkspaceOpenStarted);
                    return Some(UiCommand::OpenWorkspace(id));
                }
            } else if activate && index == state.workspaces.len().saturating_add(1) {
                start_workspace_creation(state);
            } else if activate && index == state.workspaces.len().saturating_add(2) {
                state.overlay = Some(Overlay::ConfirmLibraryDelete);
            }
            None
        }
        Overlay::Palette => {
            let commands = filtered_palette_commands(state);
            let area = centered(64, (commands.len() as u16 + 5).clamp(9, 21), screen);
            if !contains(area, &mouse) {
                return None;
            }
            if mouse.row < area.y.saturating_add(3) {
                set_editor_cursor_from_column(
                    &mut state.palette.query,
                    mouse.column.saturating_sub(area.x) as usize,
                );
                return None;
            }
            let index = mouse.row.saturating_sub(area.y.saturating_add(4)) as usize;
            if index < commands.len() {
                state.palette.cursor = index;
                if activate {
                    return execute_palette(state, commands[index]);
                }
            }
            None
        }
        Overlay::ModelManager => handle_model_manager_mouse(state, mouse, screen, activate),
        Overlay::ConfirmModelDelete => {
            handle_delete_model_confirm_mouse(state, mouse, screen, activate)
        }
        Overlay::FileBrowser => handle_file_browser_mouse(state, mouse, screen, activate),
        Overlay::ConfirmImport => handle_confirm_import_mouse(state, mouse, screen, activate),
        Overlay::DocumentDetails => {
            let area = centered(66, 19, screen);
            if activate && contains(area, &mouse) && mouse.row >= area.bottom().saturating_sub(3) {
                if mouse.column < area.x + area.width / 2 {
                    return selected_library_document(state).map(|document| UiCommand::OpenPdf {
                        path: document.source.clone(),
                        page: None,
                    });
                }
                if let Some(document) = selected_library_document(state) {
                    let tags = state
                        .document_tags
                        .get(&document.id)
                        .cloned()
                        .unwrap_or_default()
                        .join(", ");
                    state.tag_editor.set(tags);
                    state.overlay = Some(Overlay::DocumentTags);
                }
            }
            None
        }
        Overlay::ConfirmDocumentDelete => {
            let area = centered(56, 10, screen);
            if activate && contains(area, &mouse) && mouse.row >= area.bottom().saturating_sub(3) {
                if mouse.column < area.x + area.width / 2 {
                    delete_selected_document(state)
                } else {
                    state.overlay = None;
                    None
                }
            } else {
                None
            }
        }
        Overlay::ConfirmLibraryDelete => {
            let area = centered(62, 12, screen);
            if activate && contains(area, &mouse) && mouse.row >= area.bottom().saturating_sub(3) {
                if mouse.column < area.x + area.width / 2 {
                    delete_selected_library(state, false)
                } else {
                    state.overlay = Some(Overlay::Workspaces);
                    None
                }
            } else {
                None
            }
        }
        Overlay::WorkspaceProfile => {
            let area = centered(78, 20, screen);
            if contains(area, &mouse) {
                let index = mouse.row.saturating_sub(area.y + 2) as usize;
                if index < state.profile_count() {
                    state.profile_cursor = index;
                }
                if activate && index < state.profile_count() {
                    return handle_workspace_profile(
                        state,
                        KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
                    );
                }
            }
            None
        }
        Overlay::CustomProfileEditor => handle_custom_profile_mouse(state, mouse, screen, activate),
        Overlay::ChatHistory => {
            let area = centered(72, 22, screen);
            if contains(area, &mouse) {
                let index = mouse.row.saturating_sub(area.y + 2) as usize;
                if index < active_sessions(state).len() {
                    state.history_cursor = index;
                }
                if activate {
                    return handle_chat_history(
                        state,
                        KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
                    );
                }
            }
            None
        }
        Overlay::DocumentTags => {
            let area = centered(58, 9, screen);
            if activate && contains(area, &mouse) && mouse.row >= area.bottom().saturating_sub(2) {
                return handle_document_tags(
                    state,
                    KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
                );
            }
            None
        }
    }
}

fn handle_file_browser_mouse(
    state: &mut AppState,
    mouse: MouseEvent,
    screen: Rect,
    activate: bool,
) -> Option<UiCommand> {
    let [_area, list, selected, footer] = file_browser_areas(screen);
    if contains(list, &mouse) {
        let index = mouse.row.saturating_sub(list.y.saturating_add(1)) as usize;
        if index < state.file_browser.entries.len() {
            state.file_browser.cursor = index;
        }
        return None;
    }
    if contains(selected, &mouse) {
        let index = mouse.row.saturating_sub(selected.y.saturating_add(1)) as usize;
        if activate && index < state.file_browser.selected.len() {
            state.file_browser.selected.remove(index);
        } else if activate {
            let base = state.file_browser.selected.len().max(1);
            let favorite_start = base + 1;
            let favorite_end = favorite_start + state.file_browser.favorites.len().min(4);
            let recent_start = if state.file_browser.favorites.is_empty() {
                base + 1
            } else {
                favorite_end + 1
            };
            let directory = if (favorite_start..favorite_end).contains(&index) {
                state
                    .file_browser
                    .favorites
                    .get(index - favorite_start)
                    .cloned()
            } else if index >= recent_start {
                state
                    .file_browser
                    .history
                    .get(index - recent_start)
                    .cloned()
            } else {
                None
            };
            if let Some(directory) = directory.filter(|directory| Path::new(directory).is_dir()) {
                state.file_browser.current_dir = directory;
                state.file_browser.cursor = 0;
                refresh_file_browser(state);
            }
        }
        return None;
    }
    if contains(footer, &mouse) && activate {
        return match compact_control_at(
            mouse.column,
            footer.x.saturating_add(2),
            &["Open", "Toggle", "Import", "Cancel"],
            0,
            4,
        ) {
            Some(0) => {
                enter_file_browser_directory(state);
                None
            }
            Some(1) => {
                toggle_file_browser_selection(state);
                None
            }
            Some(2) => request_import_confirmation(state),
            Some(3) => {
                update(state, Action::CloseOverlay);
                None
            }
            _ => None,
        };
    }
    None
}

fn handle_confirm_import_mouse(
    state: &mut AppState,
    mouse: MouseEvent,
    screen: Rect,
    activate: bool,
) -> Option<UiCommand> {
    let area = confirm_import_area(screen);
    if !contains(area, &mouse) || !activate || mouse.row != area.bottom().saturating_sub(2) {
        return None;
    }
    match compact_control_at(
        mouse.column,
        area.x.saturating_add(3),
        &["Enter / Y  Queue import", "Esc / N  Back"],
        0,
        6,
    ) {
        Some(0) => confirm_file_browser_import(state),
        Some(1) => {
            state.overlay = Some(Overlay::FileBrowser);
            None
        }
        _ => None,
    }
}

fn handle_delete_model_confirm_mouse(
    state: &mut AppState,
    mouse: MouseEvent,
    screen: Rect,
    activate: bool,
) -> Option<UiCommand> {
    let area = delete_model_confirm_area(screen);
    if !contains(area, &mouse) || !activate || mouse.row != area.bottom().saturating_sub(2) {
        return None;
    }
    match compact_control_at(
        mouse.column,
        area.x.saturating_add(3),
        &["Enter / Y  Delete", "Esc / N  Cancel"],
        0,
        8,
    ) {
        Some(0) => confirm_model_delete(state),
        Some(1) => {
            cancel_model_delete(state);
            None
        }
        _ => None,
    }
}

fn handle_model_manager_mouse(
    state: &mut AppState,
    mouse: MouseEvent,
    screen: Rect,
    activate: bool,
) -> Option<UiCommand> {
    let [area, sidebar, search, list, _details, footer] = model_manager_areas(screen);
    if !contains(area, &mouse) {
        return None;
    }
    if contains(sidebar, &mouse) {
        let row = mouse.row.saturating_sub(sidebar.y);
        if (1..=3).contains(&row) {
            let source = [
                ModelSource::Installed,
                ModelSource::Ollama,
                ModelSource::HuggingFace,
            ][row.saturating_sub(1) as usize];
            if source != state.model_manager.source {
                state.model_manager.source = source;
                return Some(refresh_model_catalog_command(state));
            }
        } else if (6..=9).contains(&row) {
            let category = [
                ModelCategory::Chat,
                ModelCategory::Vl,
                ModelCategory::Embedding,
                ModelCategory::Rerank,
            ][row.saturating_sub(6) as usize];
            if category != state.model_manager.category {
                state.model_manager.category = category;
                return Some(refresh_model_catalog_command(state));
            }
        } else if row >= 12 {
            let index = row.saturating_sub(12) as usize;
            if index < state.model_manager.packages.len() {
                state.model_manager.package_cursor = index;
            }
        }
        return None;
    }
    if contains(search, &mouse) {
        state.model_manager.searching = true;
        set_editor_cursor_from_column(
            &mut state.model_manager.query,
            mouse.column.saturating_sub(search.x.saturating_add(11)) as usize,
        );
        return None;
    }
    if contains(list, &mouse) {
        let index = mouse.row.saturating_sub(list.y) as usize;
        if index < state.model_manager.entries.len() {
            state.model_manager.cursor = index;
        }
        return None;
    }
    if contains(footer, &mouse) && activate {
        if mouse.row == footer.y.saturating_add(1) {
            let controls = [
                format!("Fit {}", state.model_manager.profile.label()),
                format!("Quant {}", state.model_manager.quantization.label()),
                format!("Context {}K", state.model_manager.context_tokens / 1024),
                format!("Purge {}", state.model_manager.memory_policy.label()),
            ];
            let labels = controls.iter().map(String::as_str).collect::<Vec<_>>();
            match compact_control_at(mouse.column, footer.x, &labels, 0, 2) {
                Some(0) => {
                    apply_next_profile(state);
                    return Some(refresh_model_catalog_command(state));
                }
                Some(1) => {
                    state.model_manager.quantization = state.model_manager.quantization.next();
                    return Some(refresh_model_catalog_command(state));
                }
                Some(2) => {
                    state.model_manager.context_tokens = match state.model_manager.context_tokens {
                        4_096 => 8_192,
                        8_192 => 16_384,
                        16_384 => 32_768,
                        _ => 4_096,
                    };
                    return Some(refresh_model_catalog_command(state));
                }
                Some(3) => {
                    state.model_manager.memory_policy = state.model_manager.memory_policy.next()
                }
                _ => {}
            }
        } else if mouse.row == footer.y.saturating_add(2) {
            return match compact_control_at(
                mouse.column,
                footer.x,
                &[
                    "Download",
                    "Load temporarily",
                    "Unload",
                    "Refresh",
                    "Apply stack",
                    "Delete",
                ],
                0,
                3,
            ) {
                Some(0) => pull_selected_model(state),
                Some(1) => load_or_pull_selected_model(state),
                Some(2) => unload_selected_model(state),
                Some(3) => Some(refresh_model_catalog_command(state)),
                Some(4) => pull_selected_package(state),
                Some(5) => request_model_delete(state),
                _ => None,
            };
        }
    }
    None
}

fn handle_mouse_scroll(state: &mut AppState, mouse: MouseEvent, screen: Rect) {
    let next = matches!(mouse.kind, MouseEventKind::ScrollDown);
    match state.overlay {
        Some(Overlay::Palette) => {
            let count = filtered_palette_commands(state).len();
            if count > 0 {
                state.palette.cursor = if next {
                    (state.palette.cursor + 1) % count
                } else {
                    (state.palette.cursor + count - 1) % count
                };
            }
        }
        Some(Overlay::Workspaces) => {
            update(
                state,
                if next {
                    Action::SelectNextWorkspace
                } else {
                    Action::SelectPreviousWorkspace
                },
            );
        }
        Some(Overlay::Help) => {}
        Some(Overlay::ModelManager) => move_model_cursor(state, next),
        Some(Overlay::ConfirmModelDelete) => {}
        Some(Overlay::FileBrowser) => move_file_browser_cursor(state, next),
        Some(Overlay::ConfirmImport) => {}
        Some(Overlay::DocumentDetails | Overlay::ConfirmDocumentDelete) => {}
        Some(Overlay::ConfirmLibraryDelete) => {}
        Some(Overlay::WorkspaceProfile) => {
            let key = if next { KeyCode::Down } else { KeyCode::Up };
            let _ = handle_workspace_profile(state, KeyEvent::new(key, KeyModifiers::NONE));
        }
        Some(Overlay::CustomProfileEditor) => {
            state.custom_profile_field = if next {
                (state.custom_profile_field + 1) % 4
            } else {
                (state.custom_profile_field + 3) % 4
            };
        }
        Some(Overlay::ChatHistory) => {
            let key = if next { KeyCode::Down } else { KeyCode::Up };
            let _ = handle_chat_history(state, KeyEvent::new(key, KeyModifiers::NONE));
        }
        Some(Overlay::DocumentTags) => {}
        None => {
            let [_header, body, _footer] = screen_areas(screen);
            let panels = dashboard_areas(body);
            let focuses = [
                FocusPanel::Chat,
                FocusPanel::Sources,
                FocusPanel::Models,
                FocusPanel::Activity,
            ];
            if let Some((_, focus)) = panels
                .iter()
                .zip(focuses)
                .find(|(area, _)| contains(**area, &mouse))
            {
                update(state, Action::SetFocus(focus));
                update(state, Action::SetInputMode(InputMode::Nav));
                move_selection(state, next);
            }
        }
    }
}

fn contains(area: Rect, mouse: &MouseEvent) -> bool {
    mouse.column >= area.x
        && mouse.column < area.right()
        && mouse.row >= area.y
        && mouse.row < area.bottom()
}

fn compact_control_at(
    column: u16,
    start: u16,
    labels: &[&str],
    horizontal_padding: u16,
    gap: u16,
) -> Option<usize> {
    let mut cursor = start;
    for (index, label) in labels.iter().enumerate() {
        let width = u16::try_from(label.chars().count())
            .unwrap_or(u16::MAX)
            .saturating_add(horizontal_padding.saturating_mul(2));
        if column >= cursor && column < cursor.saturating_add(width) {
            return Some(index);
        }
        cursor = cursor.saturating_add(width).saturating_add(gap);
    }
    None
}

fn bordered_inner(area: Rect) -> Rect {
    Rect::new(
        area.x.saturating_add(1),
        area.y.saturating_add(1),
        area.width.saturating_sub(2),
        area.height.saturating_sub(2),
    )
}

fn set_editor_cursor_from_column(editor: &mut EditorState, column: usize) {
    let mut width = 0;
    editor.cursor = editor.value.len();
    for (index, character) in editor.value.char_indices() {
        let character_width = character.width().unwrap_or(0);
        if width + character_width > column {
            editor.cursor = index;
            break;
        }
        width += character_width;
    }
}

fn handle_paste(state: &mut AppState, text: &str) {
    if state.overlay == Some(Overlay::Workspaces) && state.creating_workspace {
        state.workspace_name.insert_str(text);
    } else if state.overlay == Some(Overlay::CustomProfileEditor) && state.custom_profile_field == 0
    {
        state.custom_profile_name.insert_str(text);
    } else if state.overlay == Some(Overlay::ModelManager) && state.model_manager.searching {
        state.model_manager.query.insert_str(text);
    } else if state.overlay == Some(Overlay::Palette) {
        state.palette.query.insert_str(text);
        state.palette.cursor = 0;
    } else if state.input_mode == InputMode::Text {
        active_editor_mut(state).insert_str(text);
        refresh_path_suggestions(state);
        if state.route == Route::Settings {
            state.config_dirty = true;
        }
    }
}

fn handle_key(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    if let Some(overlay) = state.overlay {
        return handle_overlay(state, overlay, key);
    }
    if state.input_mode == InputMode::Text
        && state.route == Route::Settings
        && key.modifiers.contains(KeyModifiers::CONTROL)
        && key.code == KeyCode::Enter
    {
        return save_config(state);
    }
    if key.modifiers.contains(KeyModifiers::CONTROL)
        && let Some(command) = handle_ctrl_shortcut(state, key.code)
    {
        return command;
    }
    if state.input_mode == InputMode::Text
        && state.route == Route::Library
        && !state.library.filtering
        && key.code == KeyCode::Tab
    {
        accept_path_suggestion(state);
        return None;
    }
    if matches!(key.code, KeyCode::Tab | KeyCode::BackTab) {
        update(state, Action::SetInputMode(InputMode::Nav));
        update(
            state,
            if key.code == KeyCode::Tab {
                Action::FocusNext
            } else {
                Action::FocusPrevious
            },
        );
        return None;
    }
    if state.input_mode == InputMode::Text {
        return handle_text(state, key);
    }
    handle_navigation(state, key)
}

fn handle_ctrl_shortcut(state: &mut AppState, code: KeyCode) -> Option<Option<UiCommand>> {
    let command = match code {
        KeyCode::Char('c') => {
            update(state, Action::Navigate(Route::Chat));
            None
        }
        KeyCode::Char('s') => {
            update(state, Action::SetFocus(FocusPanel::Sources));
            update(state, Action::SetInputMode(InputMode::Nav));
            None
        }
        KeyCode::Char('l') => {
            state.creating_workspace = false;
            state.overlay = Some(Overlay::Workspaces);
            None
        }
        KeyCode::Char('h') => {
            update(state, Action::SetFocus(FocusPanel::Models));
            update(state, Action::SetInputMode(InputMode::Nav));
            None
        }
        KeyCode::Char('m') => {
            update(state, Action::SetFocus(FocusPanel::Models));
            update(state, Action::SetInputMode(InputMode::Nav));
            None
        }
        KeyCode::Char('a') => {
            update(state, Action::Navigate(Route::Jobs));
            None
        }
        KeyCode::Char('t') => {
            update(state, Action::CycleTheme);
            None
        }
        KeyCode::Char('w') => {
            update(state, Action::OpenOverlay(Overlay::Workspaces));
            None
        }
        KeyCode::Char('p') => {
            update(state, Action::OpenOverlay(Overlay::Palette));
            None
        }
        KeyCode::Char('q') => {
            update(state, Action::QuitRequested);
            None
        }
        KeyCode::Char('i') => {
            open_file_browser(state);
            None
        }
        KeyCode::Char('n') => {
            start_workspace_creation(state);
            None
        }
        KeyCode::Char('r') => Some(UiCommand::RefreshJobs),
        KeyCode::Char('x') => cancel_active(state),
        KeyCode::Char('d') => {
            update(state, Action::NotificationDismissed);
            None
        }
        KeyCode::Char('e') => {
            cycle_evidence_mode(state);
            None
        }
        KeyCode::Char('z') => return Some(undo_last_action(state)),
        _ => return None,
    };
    Some(command)
}

fn visible_library_jobs(state: &AppState) -> Vec<&omarag_domain::JobSnapshot> {
    state
        .jobs
        .values()
        .filter(|job| {
            job.kind == "ingest"
                && !state.hidden_jobs.contains(&job.id)
                && !matches!(job.status, JobStatus::Completed | JobStatus::Cancelled)
                && matches!(
                    state.library.filter,
                    LibraryFilter::All | LibraryFilter::Indexing
                )
                .then_some(!matches!(job.status, JobStatus::Failed))
                .unwrap_or(false)
                || (job.kind == "ingest"
                    && !state.hidden_jobs.contains(&job.id)
                    && job.status == JobStatus::Failed
                    && matches!(
                        state.library.filter,
                        LibraryFilter::All | LibraryFilter::Failed
                    ))
        })
        .collect()
}

fn visible_document_indices(state: &AppState) -> Vec<usize> {
    let query = state.library.query.value.trim();
    let mut indices = state
        .documents
        .iter()
        .enumerate()
        .filter(|(_, document)| {
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
            let query_match = fuzzy_score(&search_text, query).is_some();
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
            let filter_match = match state.library.filter {
                LibraryFilter::All | LibraryFilter::Ready => true,
                LibraryFilter::Duplicates => duplicate,
                LibraryFilter::Indexing | LibraryFilter::Failed => false,
            };
            query_match && filter_match
        })
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    indices.sort_by(|left, right| {
        let left = &state.documents[*left];
        let right = &state.documents[*right];
        if !query.is_empty() {
            let search_text = |document: &DocumentSummary| {
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
            omarag_app::LibrarySort::Newest => right.imported_at.cmp(&left.imported_at),
            omarag_app::LibrarySort::Title => {
                left.title.to_lowercase().cmp(&right.title.to_lowercase())
            }
            omarag_app::LibrarySort::Size => {
                let left_size = state
                    .library
                    .details
                    .get(&left.id)
                    .map_or(0, |detail| detail.size_bytes);
                let right_size = state
                    .library
                    .details
                    .get(&right.id)
                    .map_or(0, |detail| detail.size_bytes);
                right_size.cmp(&left_size)
            }
        }
    });
    indices
}

fn selected_library_job(state: &AppState) -> Option<&omarag_domain::JobSnapshot> {
    visible_library_jobs(state).get(state.asset_cursor).copied()
}

fn selected_library_document(state: &AppState) -> Option<&DocumentSummary> {
    let jobs = visible_library_jobs(state).len();
    let index = state.asset_cursor.checked_sub(jobs)?;
    let document = *visible_document_indices(state).get(index)?;
    state.documents.get(document)
}

fn sync_document_cursor(state: &mut AppState) {
    if let Some(document) = selected_library_document(state)
        && let Some(index) = state
            .documents
            .iter()
            .position(|item| item.id == document.id)
    {
        state.document_cursor = index;
    }
}

fn toggle_selected_library_job(state: &AppState) -> Option<UiCommand> {
    let job = selected_library_job(state)?;
    let command = match job.status {
        JobStatus::Running | JobStatus::Queued => JobCommand::Pause,
        JobStatus::Paused | JobStatus::PauseRequested => JobCommand::Resume,
        _ => return None,
    };
    Some(UiCommand::Job {
        id: job.id.clone(),
        command,
    })
}

fn cancel_selected_library_job(state: &mut AppState) -> Option<UiCommand> {
    let job = selected_library_job(state)?.clone();
    let command = (!matches!(
        job.status,
        JobStatus::Completed | JobStatus::Cancelled | JobStatus::Failed
    ))
    .then(|| UiCommand::Job {
        id: job.id.clone(),
        command: JobCommand::Cancel,
    });
    if command.is_some() {
        state.undo = Some(UndoAction::CancelledJob(job));
    }
    command
}

fn retry_selected_library_job(state: &AppState) -> Option<UiCommand> {
    let job = selected_library_job(state)?;
    if job.status != JobStatus::Failed {
        return None;
    }
    let request = serde_json::from_value::<IngestRequest>(job.payload.clone()).ok()?;
    Some(UiCommand::Ingest {
        workspace: job.workspace_id.clone(),
        request,
    })
}

fn hide_selected_library_job(state: &mut AppState) {
    if let Some(job) = selected_library_job(state).cloned() {
        state.hidden_jobs.insert(job.id.clone());
        state.undo = Some(UndoAction::HiddenJob(job));
        state.asset_cursor = state.asset_cursor.saturating_sub(1);
    }
}

fn move_citation(state: &mut AppState, next: bool) {
    let len = state.chat.citations.len().min(4);
    if len == 0 {
        return;
    }
    state.citation_cursor = if next {
        (state.citation_cursor + 1) % len
    } else {
        (state.citation_cursor + len - 1) % len
    };
    state.gallery_cursor = state.citation_cursor;
}

fn selected_citation_command(state: &AppState, preview: bool) -> Option<UiCommand> {
    let citation = state.chat.citations.get(state.citation_cursor)?;
    let (path, page) = citation_pdf_target(state, citation)?;
    Some(if preview {
        UiCommand::OpenPageImage {
            path,
            page,
            primary_anchors: citation.primary_anchors.clone(),
            context_anchors: citation.context_anchors.clone(),
        }
    } else {
        UiCommand::OpenPdf {
            path,
            page: Some(page),
        }
    })
}

fn selected_citation_copy(state: &AppState) -> Option<UiCommand> {
    let citation = state.chat.citations.get(state.citation_cursor)?;
    let (path, page) = citation_pdf_target(state, citation)?;
    Some(UiCommand::CopyText(format!("{path}#page={page}")))
}

fn repeat_current_question(state: &mut AppState) -> Option<UiCommand> {
    let workspace = state.active_workspace.clone()?;
    let question = state.chat.question.value.trim().to_owned();
    if question.is_empty() {
        return None;
    }
    update(state, Action::RunRequestStarted);
    Some(UiCommand::StartRun {
        workspace,
        question,
        evidence_mode: state.chat.evidence_mode,
    })
}

fn export_current_chat(state: &AppState) -> Option<UiCommand> {
    let workspace_id = state.active_workspace.clone()?;
    let workspace = state
        .workspaces
        .iter()
        .find(|item| item.id == workspace_id)
        .map_or_else(|| workspace_id.clone(), |item| item.name.clone());
    Some(UiCommand::ExportChat {
        workspace,
        session: ChatSession {
            workspace_id,
            question: state.chat.question.value.clone(),
            answer: state.chat.answer.clone(),
            citations: state.chat.citations.clone(),
            created_at: "now".into(),
        },
    })
}

fn undo_last_action(state: &mut AppState) -> Option<UiCommand> {
    match state.undo.take()? {
        UndoAction::RemovedDocument(document) => {
            state
                .active_workspace
                .clone()
                .map(|workspace| UiCommand::RestoreDocument {
                    workspace,
                    document,
                })
        }
        UndoAction::HiddenJob(job) => {
            state.hidden_jobs.remove(&job.id);
            None
        }
        UndoAction::CancelledJob(job) => {
            let request = serde_json::from_value::<IngestRequest>(job.payload).ok()?;
            Some(UiCommand::Ingest {
                workspace: job.workspace_id,
                request,
            })
        }
        UndoAction::ProfileChanged {
            workspace,
            previous,
            previous_custom,
        } => {
            state.workspace_profiles.insert(workspace.clone(), previous);
            if let Some(custom) = previous_custom {
                state.workspace_custom_profiles.insert(workspace, custom);
            } else {
                state.workspace_custom_profiles.remove(&workspace);
            }
            None
        }
        UndoAction::WorkspaceChanged(workspace) => Some(UiCommand::OpenWorkspace(workspace)),
    }
}

fn handle_overlay(state: &mut AppState, overlay: Overlay, key: KeyEvent) -> Option<UiCommand> {
    match overlay {
        Overlay::Help => {
            if matches!(key.code, KeyCode::Esc | KeyCode::Enter | KeyCode::Char('?')) {
                update(state, Action::CloseOverlay);
            }
            None
        }
        Overlay::Workspaces => handle_workspace_overlay(state, key),
        Overlay::Palette => handle_palette(state, key),
        Overlay::ModelManager => handle_model_manager(state, key),
        Overlay::ConfirmModelDelete => match key.code {
            KeyCode::Enter | KeyCode::Char('y') => confirm_model_delete(state),
            KeyCode::Esc | KeyCode::Char('n') => {
                cancel_model_delete(state);
                None
            }
            _ => None,
        },
        Overlay::FileBrowser => handle_file_browser(state, key),
        Overlay::ConfirmImport => match key.code {
            KeyCode::Enter | KeyCode::Char('y') => confirm_file_browser_import(state),
            KeyCode::Esc | KeyCode::Char('n') => {
                state.overlay = Some(Overlay::FileBrowser);
                None
            }
            _ => None,
        },
        Overlay::DocumentDetails => match key.code {
            KeyCode::Esc | KeyCode::Char('i') => {
                state.overlay = None;
                None
            }
            KeyCode::Enter | KeyCode::Char('o') => {
                selected_library_document(state).map(|document| UiCommand::OpenPdf {
                    path: document.source.clone(),
                    page: None,
                })
            }
            KeyCode::Char('t') => {
                if let Some(document) = selected_library_document(state) {
                    let tags = state
                        .document_tags
                        .get(&document.id)
                        .cloned()
                        .unwrap_or_default()
                        .join(", ");
                    state.tag_editor.set(tags);
                    state.overlay = Some(Overlay::DocumentTags);
                }
                None
            }
            _ => None,
        },
        Overlay::ConfirmDocumentDelete => match key.code {
            KeyCode::Enter | KeyCode::Char('y') => delete_selected_document(state),
            KeyCode::Esc | KeyCode::Char('n') => {
                state.overlay = None;
                None
            }
            _ => None,
        },
        Overlay::WorkspaceProfile => handle_workspace_profile(state, key),
        Overlay::CustomProfileEditor => handle_custom_profile_editor(state, key),
        Overlay::ConfirmLibraryDelete => handle_library_delete_confirmation(state, key),
        Overlay::ChatHistory => handle_chat_history(state, key),
        Overlay::DocumentTags => handle_document_tags(state, key),
    }
}

fn handle_document_tags(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    match key.code {
        KeyCode::Esc => state.overlay = None,
        KeyCode::Enter => {
            if let Some(document) = selected_library_document(state) {
                let id = document.id.clone();
                let tags = state
                    .tag_editor
                    .value
                    .split(',')
                    .map(str::trim)
                    .filter(|tag| !tag.is_empty())
                    .map(str::to_owned)
                    .collect::<Vec<_>>();
                state.document_tags.insert(id, tags);
                state.overlay = None;
            }
        }
        KeyCode::Backspace => state.tag_editor.backspace(),
        KeyCode::Delete => state.tag_editor.delete(),
        KeyCode::Left => state.tag_editor.move_left(),
        KeyCode::Right => state.tag_editor.move_right(),
        KeyCode::Home => state.tag_editor.home(),
        KeyCode::End => state.tag_editor.end(),
        KeyCode::Char(character)
            if !key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
        {
            state.tag_editor.insert_char(character)
        }
        _ => {}
    }
    None
}

fn delete_selected_document(state: &mut AppState) -> Option<UiCommand> {
    let document = selected_library_document(state)?.clone();
    let workspace = state.active_workspace.clone()?;
    state.overlay = None;
    update(
        state,
        Action::OperationStarted("Removing document from index".into()),
    );
    Some(UiCommand::DeleteDocument {
        workspace,
        document,
    })
}

fn handle_workspace_profile(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    let count = state.profile_count().max(1);
    match key.code {
        KeyCode::Esc | KeyCode::Char('b') => {
            state.overlay = if state.creating_workspace {
                Some(Overlay::Workspaces)
            } else {
                None
            }
        }
        KeyCode::Up | KeyCode::Char('k') => {
            state.profile_cursor = (state.profile_cursor + count - 1) % count
        }
        KeyCode::Down | KeyCode::Char('j') => {
            state.profile_cursor = (state.profile_cursor + 1) % count
        }
        KeyCode::Char('c') => start_custom_profile_editor(state, None),
        KeyCode::Char('e') => {
            let custom_index = state
                .profile_cursor
                .checked_sub(WorkspaceProfile::ALL.len());
            if custom_index.is_some_and(|index| index < state.custom_profiles.len()) {
                start_custom_profile_editor(state, custom_index);
            }
        }
        KeyCode::Enter | KeyCode::Char('a') => {
            if state.creating_workspace {
                state.overlay = Some(Overlay::Workspaces);
            } else if let Some(workspace) = state.active_workspace.clone() {
                let previous = state.active_profile();
                let previous_custom = state.workspace_custom_profiles.get(&workspace).cloned();
                state.assign_profile_at(workspace.clone(), state.profile_cursor);
                state.undo = Some(UndoAction::ProfileChanged {
                    workspace,
                    previous,
                    previous_custom,
                });
                state.overlay = None;
            }
        }
        _ => {}
    }
    None
}

fn start_custom_profile_editor(state: &mut AppState, edit: Option<usize>) {
    let draft = edit
        .and_then(|index| state.custom_profiles.get(index).cloned())
        .unwrap_or_else(|| CustomLibraryProfile {
            id: String::new(),
            name: "My profile".into(),
            processing_profile: "default".into(),
            duplicate_policy: "review".into(),
            validity_policy: "prefer-current".into(),
        });
    state.custom_profile_name.set(draft.name.clone());
    state.custom_profile_draft = draft;
    state.custom_profile_field = 0;
    state.editing_custom_profile = edit;
    state.overlay = Some(Overlay::CustomProfileEditor);
}

fn cycle_custom_profile_value(state: &mut AppState, next: bool) {
    fn cycle(current: &mut String, values: &[&str], next: bool) {
        let position = values
            .iter()
            .position(|value| *value == current)
            .unwrap_or(0);
        let index = if next {
            (position + 1) % values.len()
        } else {
            (position + values.len() - 1) % values.len()
        };
        *current = values[index].into();
    }
    match state.custom_profile_field {
        1 => cycle(
            &mut state.custom_profile_draft.processing_profile,
            &[
                "default",
                "technical",
                "image-heavy",
                "low-memory",
                "fast",
                "quality",
            ],
            next,
        ),
        2 => cycle(
            &mut state.custom_profile_draft.duplicate_policy,
            &["review", "skip", "replace"],
            next,
        ),
        3 => cycle(
            &mut state.custom_profile_draft.validity_policy,
            &["prefer-current", "strict", "allow-stale"],
            next,
        ),
        _ => {}
    }
}

fn save_custom_profile(state: &mut AppState) {
    let name = state.custom_profile_name.value.trim();
    if name.is_empty() {
        notify(state, NotificationLevel::Warning, "Enter a profile name.");
        return;
    }
    state.custom_profile_draft.name = name.into();
    let index = if let Some(index) = state.editing_custom_profile {
        state.custom_profiles[index] = state.custom_profile_draft.clone();
        index
    } else {
        let mut number = state.custom_profiles.len() + 1;
        loop {
            let candidate = format!("custom-{number}");
            if state
                .custom_profiles
                .iter()
                .all(|profile| profile.id != candidate)
            {
                state.custom_profile_draft.id = candidate;
                break;
            }
            number += 1;
        }
        state
            .custom_profiles
            .push(state.custom_profile_draft.clone());
        state.custom_profiles.len() - 1
    };
    state.profile_cursor = WorkspaceProfile::ALL.len() + index;
    state.overlay = Some(Overlay::WorkspaceProfile);
}

fn handle_custom_profile_editor(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('s') {
        save_custom_profile(state);
        return None;
    }
    match key.code {
        KeyCode::Esc => state.overlay = Some(Overlay::WorkspaceProfile),
        KeyCode::Tab | KeyCode::Down => {
            state.custom_profile_field = (state.custom_profile_field + 1) % 4
        }
        KeyCode::BackTab | KeyCode::Up => {
            state.custom_profile_field = (state.custom_profile_field + 3) % 4
        }
        KeyCode::Left if state.custom_profile_field == 0 => state.custom_profile_name.move_left(),
        KeyCode::Right if state.custom_profile_field == 0 => state.custom_profile_name.move_right(),
        KeyCode::Left => cycle_custom_profile_value(state, false),
        KeyCode::Right | KeyCode::Char(' ') => cycle_custom_profile_value(state, true),
        KeyCode::Backspace if state.custom_profile_field == 0 => {
            state.custom_profile_name.backspace()
        }
        KeyCode::Delete if state.custom_profile_field == 0 => state.custom_profile_name.delete(),
        KeyCode::Home if state.custom_profile_field == 0 => state.custom_profile_name.home(),
        KeyCode::End if state.custom_profile_field == 0 => state.custom_profile_name.end(),
        KeyCode::Enter => save_custom_profile(state),
        KeyCode::Char(character)
            if state.custom_profile_field == 0
                && !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
        {
            state.custom_profile_name.insert_char(character)
        }
        _ => {}
    }
    None
}

fn handle_custom_profile_mouse(
    state: &mut AppState,
    mouse: MouseEvent,
    screen: Rect,
    activate: bool,
) -> Option<UiCommand> {
    let area = centered(66, 18, screen);
    if !contains(area, &mouse) {
        return None;
    }
    let row = mouse.row.saturating_sub(area.y + 4) as usize;
    if row < 4 {
        state.custom_profile_field = row;
        if activate && row > 0 {
            cycle_custom_profile_value(state, true);
        }
    } else if activate && mouse.row >= area.bottom().saturating_sub(3) {
        if mouse.column < area.x + area.width / 2 {
            save_custom_profile(state);
        } else {
            state.overlay = Some(Overlay::WorkspaceProfile);
        }
    }
    None
}

fn handle_library_delete_confirmation(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    match key.code {
        KeyCode::Esc | KeyCode::Char('n') => {
            state.overlay = Some(Overlay::Workspaces);
            None
        }
        KeyCode::Enter | KeyCode::Char('u') => delete_selected_library(state, false),
        KeyCode::Char('D') => delete_selected_library(state, true),
        _ => None,
    }
}

fn delete_selected_library(state: &mut AppState, physical: bool) -> Option<UiCommand> {
    let id = selected_workspace(state)?;
    state.overlay = None;
    update(
        state,
        Action::OperationStarted(if physical {
            "Deleting library files".into()
        } else {
            "Removing library".into()
        }),
    );
    Some(UiCommand::DeleteLibrary { id, physical })
}

fn active_sessions(state: &AppState) -> &[ChatSession] {
    state
        .active_workspace
        .as_ref()
        .and_then(|workspace| state.chat_sessions.get(workspace))
        .map_or(&[], Vec::as_slice)
}

fn handle_chat_history(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    let len = active_sessions(state).len();
    match key.code {
        KeyCode::Esc | KeyCode::Char('h') => state.overlay = None,
        KeyCode::Up | KeyCode::Char('k') if len > 0 => {
            state.history_cursor = (state.history_cursor + len - 1) % len
        }
        KeyCode::Down | KeyCode::Char('j') if len > 0 => {
            state.history_cursor = (state.history_cursor + 1) % len
        }
        KeyCode::Enter | KeyCode::Char('e') => {
            if let Some(session) = active_sessions(state).get(state.history_cursor).cloned() {
                state.chat.question.set(session.question);
                if key.code == KeyCode::Enter {
                    state.chat.answer = session.answer;
                    state.chat.citations = session.citations;
                    state.citation_cursor = 0;
                    state.overlay = None;
                } else {
                    state.input_mode = InputMode::Text;
                    state.overlay = None;
                }
            }
        }
        KeyCode::Char('r') => {
            if let Some(session) = active_sessions(state).get(state.history_cursor).cloned() {
                state.chat.question.set(session.question);
                state.overlay = None;
                return repeat_current_question(state);
            }
        }
        KeyCode::Char('x') => {
            if let Some(session) = active_sessions(state).get(state.history_cursor).cloned() {
                let workspace = state
                    .active_workspace
                    .as_ref()
                    .and_then(|id| state.workspaces.iter().find(|item| &item.id == id))
                    .map_or("Library".into(), |item| item.name.clone());
                return Some(UiCommand::ExportChat { workspace, session });
            }
        }
        _ => {}
    }
    None
}

fn handle_workspace_overlay(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    if state.creating_workspace {
        match key.code {
            KeyCode::Esc => cancel_workspace_creation(state),
            KeyCode::Enter => return create_workspace_command(state),
            KeyCode::Tab => {
                state.profile_cursor = (state.profile_cursor + 1) % state.profile_count().max(1)
            }
            KeyCode::BackTab => {
                let count = state.profile_count().max(1);
                state.profile_cursor = (state.profile_cursor + count - 1) % count
            }
            KeyCode::Char('p') if key.modifiers.contains(KeyModifiers::ALT) => {
                state.overlay = Some(Overlay::WorkspaceProfile)
            }
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::ALT) => {
                start_custom_profile_editor(state, None)
            }
            KeyCode::Backspace => state.workspace_name.backspace(),
            KeyCode::Delete => state.workspace_name.delete(),
            KeyCode::Left => state.workspace_name.move_left(),
            KeyCode::Right => state.workspace_name.move_right(),
            KeyCode::Home => state.workspace_name.home(),
            KeyCode::End => state.workspace_name.end(),
            KeyCode::Char(character)
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                state.workspace_name.insert_char(character);
            }
            _ => {}
        }
        return None;
    }
    match key.code {
        KeyCode::Esc | KeyCode::Char('w') => {
            update(state, Action::CloseOverlay);
            None
        }
        KeyCode::Char('n') => {
            start_workspace_creation(state);
            None
        }
        KeyCode::Char('d') | KeyCode::Delete => {
            if selected_workspace(state).is_some() {
                state.overlay = Some(Overlay::ConfirmLibraryDelete);
            }
            None
        }
        KeyCode::Up | KeyCode::Char('k') => {
            update(state, Action::SelectPreviousWorkspace);
            None
        }
        KeyCode::Down | KeyCode::Char('j') => {
            update(state, Action::SelectNextWorkspace);
            None
        }
        KeyCode::Enter => selected_workspace(state).map(|id| {
            if let Some(previous) = state
                .active_workspace
                .clone()
                .filter(|previous| previous != &id)
            {
                state.undo = Some(UndoAction::WorkspaceChanged(previous));
            }
            update(state, Action::WorkspaceOpenStarted);
            UiCommand::OpenWorkspace(id)
        }),
        _ => None,
    }
}

fn start_workspace_creation(state: &mut AppState) {
    state.creating_workspace = true;
    state.workspace_name = EditorState::default();
    state.profile_cursor = state.active_profile_index();
    state.overlay = Some(Overlay::Workspaces);
}

fn cancel_workspace_creation(state: &mut AppState) {
    state.creating_workspace = false;
    state.workspace_name = EditorState::default();
    state.overlay = Some(Overlay::Workspaces);
}

fn create_workspace_command(state: &mut AppState) -> Option<UiCommand> {
    let name = state.workspace_name.value.trim().to_owned();
    if name.is_empty() {
        notify(state, NotificationLevel::Warning, "Enter a library name.");
        return None;
    }
    state.operation.active = true;
    state.operation.label = format!("Creating {name}");
    Some(UiCommand::CreateWorkspace(CreateWorkspace {
        name,
        id: None,
        read_only: false,
    }))
}

fn open_file_browser(state: &mut AppState) {
    let start = if !state.file_browser.current_dir.is_empty()
        && Path::new(&state.file_browser.current_dir).is_dir()
    {
        PathBuf::from(&state.file_browser.current_dir)
    } else if let Some(home) = std::env::var_os("HOME") {
        PathBuf::from(home)
    } else {
        std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
    };
    state.file_browser.current_dir = start.to_string_lossy().into_owned();
    refresh_file_browser(state);
    state.overlay = Some(Overlay::FileBrowser);
    state.focus = FocusPanel::Import;
    state.input_mode = InputMode::Nav;
}

pub fn refresh_file_browser(state: &mut AppState) {
    let directory = PathBuf::from(&state.file_browser.current_dir);
    let mut entries = match std::fs::read_dir(&directory) {
        Ok(items) => items
            .filter_map(Result::ok)
            .filter_map(|item| {
                let path = item.path();
                let is_dir = path.is_dir();
                let is_pdf = path
                    .extension()
                    .and_then(|extension| extension.to_str())
                    .is_some_and(|extension| extension.eq_ignore_ascii_case("pdf"));
                if !is_dir && !is_pdf {
                    return None;
                }
                Some(omarag_app::FileBrowserEntry {
                    name: format!(
                        "{}{}",
                        item.file_name().to_string_lossy(),
                        if is_dir {
                            MAIN_SEPARATOR.to_string()
                        } else {
                            String::new()
                        }
                    ),
                    path: path.to_string_lossy().into_owned(),
                    is_dir,
                })
            })
            .collect::<Vec<_>>(),
        Err(error) => {
            state.file_browser.error = Some(error.to_string());
            Vec::new()
        }
    };
    entries.sort_by_key(|entry| (!entry.is_dir, entry.name.to_lowercase()));
    state.file_browser.entries = entries;
    state.file_browser.cursor = state
        .file_browser
        .cursor
        .min(state.file_browser.entries.len().saturating_sub(1));
    if !state.file_browser.entries.is_empty() {
        state.file_browser.error = None;
    }
}

fn move_file_browser_cursor(state: &mut AppState, next: bool) {
    let len = state.file_browser.entries.len();
    if len == 0 {
        return;
    }
    state.file_browser.cursor = if next {
        (state.file_browser.cursor + 1) % len
    } else {
        (state.file_browser.cursor + len - 1) % len
    };
}

fn enter_file_browser_directory(state: &mut AppState) {
    let Some(entry) = state
        .file_browser
        .entries
        .get(state.file_browser.cursor)
        .cloned()
    else {
        return;
    };
    if entry.is_dir {
        state.file_browser.current_dir = entry.path;
        state.file_browser.cursor = 0;
        refresh_file_browser(state);
    }
}

fn leave_file_browser_directory(state: &mut AppState) {
    let current = PathBuf::from(&state.file_browser.current_dir);
    if let Some(parent) = current.parent() {
        state.file_browser.current_dir = parent.to_string_lossy().into_owned();
        state.file_browser.cursor = 0;
        refresh_file_browser(state);
    }
}

fn toggle_file_browser_selection(state: &mut AppState) {
    let Some(path) = state
        .file_browser
        .entries
        .get(state.file_browser.cursor)
        .map(|entry| entry.path.clone())
    else {
        return;
    };
    if let Some(index) = state
        .file_browser
        .selected
        .iter()
        .position(|selected| selected == &path)
    {
        state.file_browser.selected.remove(index);
    } else {
        state.file_browser.selected.push(path);
    }
}

fn request_import_confirmation(state: &mut AppState) -> Option<UiCommand> {
    if state.file_browser.selected.is_empty() {
        notify(
            state,
            NotificationLevel::Warning,
            "Select at least one folder or PDF with Space.",
        );
        return None;
    }
    state.library.preflight = ImportPreflight {
        busy: true,
        selected: state.file_browser.selected.clone(),
        ..ImportPreflight::default()
    };
    state.overlay = Some(Overlay::ConfirmImport);
    Some(UiCommand::AnalyzeImport {
        selected: state.file_browser.selected.clone(),
        existing: state
            .documents
            .iter()
            .map(|document| document.source.clone())
            .collect(),
    })
}

fn confirm_file_browser_import(state: &mut AppState) -> Option<UiCommand> {
    let Some(workspace) = state.active_workspace.clone() else {
        notify(
            state,
            NotificationLevel::Warning,
            "Create or select a library first.",
        );
        state.overlay = Some(Overlay::FileBrowser);
        return None;
    };
    if state.file_browser.selected.is_empty() {
        state.overlay = Some(Overlay::FileBrowser);
        return None;
    }
    if state.library.preflight.busy {
        return None;
    }
    let paths = state
        .library
        .preflight
        .pdfs
        .iter()
        .filter(|path| {
            !state.library.preflight.unreadable.contains(path)
                && !state.library.preflight.encrypted.contains(path)
        })
        .cloned()
        .collect::<Vec<_>>();
    if paths.is_empty() {
        notify(
            state,
            NotificationLevel::Warning,
            "The selection contains no readable, unencrypted PDF files.",
        );
        state.overlay = Some(Overlay::FileBrowser);
        return None;
    }
    state.overlay = None;
    update(state, Action::ImportStarted);
    let profile = state.active_profile_settings();
    Some(UiCommand::Ingest {
        workspace,
        request: IngestRequest {
            processing_profile: profile.processing_profile,
            duplicate_policy: profile.duplicate_policy,
            validity_policy: profile.validity_policy,
            ..IngestRequest::files(paths)
        },
    })
}

pub fn expand_import_paths(selected: &[String]) -> Vec<String> {
    fn collect(directory: &Path, files: &mut Vec<String>) {
        let Ok(entries) = std::fs::read_dir(directory) else {
            return;
        };
        let mut entries = entries.flatten().collect::<Vec<_>>();
        entries.sort_by_key(|entry| entry.file_name().to_string_lossy().to_lowercase());
        for entry in entries {
            let path = entry.path();
            let Ok(metadata) = path.symlink_metadata() else {
                continue;
            };
            if metadata.file_type().is_symlink() {
                continue;
            }
            if metadata.is_dir() {
                collect(&path, files);
            } else if path
                .extension()
                .is_some_and(|extension| extension.eq_ignore_ascii_case("pdf"))
            {
                files.push(path.to_string_lossy().into_owned());
            }
        }
    }

    let mut files = Vec::new();
    for selected in selected {
        let path = Path::new(selected);
        if path.is_dir() {
            collect(path, &mut files);
        } else if path
            .extension()
            .is_some_and(|extension| extension.eq_ignore_ascii_case("pdf"))
        {
            files.push(selected.clone());
        }
    }
    files.sort();
    files.dedup();
    files
}

fn handle_file_browser(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    match key.code {
        KeyCode::Esc => {
            update(state, Action::CloseOverlay);
        }
        KeyCode::Up | KeyCode::Char('k') => move_file_browser_cursor(state, false),
        KeyCode::Down | KeyCode::Char('j') => move_file_browser_cursor(state, true),
        KeyCode::Home => state.file_browser.cursor = 0,
        KeyCode::End => {
            state.file_browser.cursor = state.file_browser.entries.len().saturating_sub(1)
        }
        KeyCode::Left | KeyCode::Char('h') | KeyCode::Backspace => {
            leave_file_browser_directory(state)
        }
        KeyCode::Right | KeyCode::Char('l') => enter_file_browser_directory(state),
        KeyCode::Char(' ') => toggle_file_browser_selection(state),
        KeyCode::Enter => {
            if state.file_browser.selected.is_empty()
                && state
                    .file_browser
                    .entries
                    .get(state.file_browser.cursor)
                    .is_some_and(|entry| entry.is_dir)
            {
                enter_file_browser_directory(state);
            } else {
                if state.file_browser.selected.is_empty() {
                    toggle_file_browser_selection(state);
                }
                return request_import_confirmation(state);
            }
        }
        KeyCode::Char('f') if key.modifiers.contains(KeyModifiers::SHIFT) => {
            cycle_saved_directory(state, true)
        }
        KeyCode::Char('r') if key.modifiers.contains(KeyModifiers::SHIFT) => {
            cycle_saved_directory(state, false)
        }
        KeyCode::Char('f') => toggle_favorite_directory(state),
        KeyCode::Char('r') => open_recent_directory(state),
        _ => {}
    }
    None
}

fn toggle_favorite_directory(state: &mut AppState) {
    let directory = state.file_browser.current_dir.clone();
    if let Some(index) = state
        .file_browser
        .favorites
        .iter()
        .position(|item| item == &directory)
    {
        state.file_browser.favorites.remove(index);
    } else {
        state.file_browser.favorites.insert(0, directory);
        state.file_browser.favorites.truncate(12);
    }
}

fn open_recent_directory(state: &mut AppState) {
    if let Some(directory) = state.file_browser.history.first().cloned()
        && Path::new(&directory).is_dir()
    {
        state.file_browser.current_dir = directory;
        state.file_browser.cursor = 0;
        refresh_file_browser(state);
    }
}

fn cycle_saved_directory(state: &mut AppState, favorites: bool) {
    let saved = if favorites {
        &state.file_browser.favorites
    } else {
        &state.file_browser.history
    };
    if saved.is_empty() {
        return;
    }
    let next = saved
        .iter()
        .position(|directory| directory == &state.file_browser.current_dir)
        .map_or(0, |index| (index + 1) % saved.len());
    let directory = saved[next].clone();
    if Path::new(&directory).is_dir() {
        state.file_browser.current_dir = directory;
        state.file_browser.cursor = 0;
        refresh_file_browser(state);
    }
}

fn handle_model_manager(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    if state.model_manager.searching {
        match key.code {
            KeyCode::Esc => state.model_manager.searching = false,
            KeyCode::Enter => {
                state.model_manager.searching = false;
                return Some(refresh_model_catalog_command(state));
            }
            KeyCode::Backspace => state.model_manager.query.backspace(),
            KeyCode::Delete => state.model_manager.query.delete(),
            KeyCode::Left => state.model_manager.query.move_left(),
            KeyCode::Right => state.model_manager.query.move_right(),
            KeyCode::Home => state.model_manager.query.home(),
            KeyCode::End => state.model_manager.query.end(),
            KeyCode::Char(character)
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                state.model_manager.query.insert_char(character);
            }
            _ => {}
        }
        return None;
    }
    match key.code {
        KeyCode::Esc => {
            update(state, Action::CloseOverlay);
            None
        }
        KeyCode::Tab => {
            state.model_manager.source = state.model_manager.source.next();
            Some(refresh_model_catalog_command(state))
        }
        KeyCode::BackTab => {
            state.model_manager.source = state.model_manager.source.previous();
            Some(refresh_model_catalog_command(state))
        }
        KeyCode::Char(']') => {
            state.model_manager.category = state.model_manager.category.next();
            Some(refresh_model_catalog_command(state))
        }
        KeyCode::Char('[') => {
            state.model_manager.category = state.model_manager.category.previous();
            Some(refresh_model_catalog_command(state))
        }
        KeyCode::Char('1'..='3') => {
            let index = match key.code {
                KeyCode::Char(value) => value.to_digit(10).unwrap_or(1) as usize - 1,
                _ => 0,
            };
            if index < state.model_manager.packages.len() {
                state.model_manager.package_cursor = index;
            }
            None
        }
        KeyCode::Char('b') => {
            if !state.model_manager.packages.is_empty() {
                state.model_manager.package_cursor =
                    (state.model_manager.package_cursor + 1) % state.model_manager.packages.len();
            }
            None
        }
        KeyCode::Right if key.modifiers.contains(KeyModifiers::SHIFT) => {
            state.model_manager.category = state.model_manager.category.next();
            Some(refresh_model_catalog_command(state))
        }
        KeyCode::Left if key.modifiers.contains(KeyModifiers::SHIFT) => {
            state.model_manager.category = state.model_manager.category.previous();
            Some(refresh_model_catalog_command(state))
        }
        KeyCode::Up | KeyCode::Char('k') => {
            move_model_cursor(state, false);
            None
        }
        KeyCode::Down | KeyCode::Char('j') => {
            move_model_cursor(state, true);
            None
        }
        KeyCode::Char('/') => {
            state.model_manager.searching = true;
            None
        }
        KeyCode::Char('q') | KeyCode::Right => {
            state.model_manager.quantization = state.model_manager.quantization.next();
            Some(refresh_model_catalog_command(state))
        }
        KeyCode::Left => {
            state.model_manager.quantization = state.model_manager.quantization.previous();
            Some(refresh_model_catalog_command(state))
        }
        KeyCode::Char('c') => {
            state.model_manager.context_tokens = match state.model_manager.context_tokens {
                4_096 => 8_192,
                8_192 => 16_384,
                16_384 => 32_768,
                _ => 4_096,
            };
            Some(refresh_model_catalog_command(state))
        }
        KeyCode::Char('f') => {
            apply_next_profile(state);
            Some(refresh_model_catalog_command(state))
        }
        KeyCode::Char('p') => {
            state.model_manager.memory_policy = state.model_manager.memory_policy.next();
            None
        }
        KeyCode::Char('r') => Some(refresh_model_catalog_command(state)),
        KeyCode::Char('a') => pull_selected_package(state),
        KeyCode::Char('d') => pull_selected_model(state),
        KeyCode::Char('l') | KeyCode::Enter => load_or_pull_selected_model(state),
        KeyCode::Char('u') => unload_selected_model(state),
        KeyCode::Char('x') | KeyCode::Delete => request_model_delete(state),
        _ => None,
    }
}

fn refresh_model_catalog_command(state: &mut AppState) -> UiCommand {
    state.model_manager.busy = true;
    state.model_manager.cursor = 0;
    state.model_manager.transfer_status = "Loading catalog".into();
    UiCommand::RefreshModelCatalog {
        source: state.model_manager.source,
        category: state.model_manager.category,
        query: state.model_manager.query.value.clone(),
        quantization: state.model_manager.quantization.label().into(),
        context_tokens: state.model_manager.context_tokens,
        profile: state.model_manager.profile,
    }
}

fn apply_next_profile(state: &mut AppState) {
    state.model_manager.profile = state.model_manager.profile.next();
    match state.model_manager.profile {
        HardwareProfile::Eco => {
            state.model_manager.quantization = ModelQuantization::Q3Km;
            state.model_manager.context_tokens = 4_096;
            state.model_manager.memory_policy = ModelMemoryPolicy::Saver;
        }
        HardwareProfile::Laptop => {
            state.model_manager.quantization = ModelQuantization::Q4Km;
            state.model_manager.context_tokens = 8_192;
            state.model_manager.memory_policy = ModelMemoryPolicy::Balanced;
        }
        HardwareProfile::Quality => {
            state.model_manager.quantization = ModelQuantization::Q5Km;
            state.model_manager.context_tokens = 8_192;
            state.model_manager.memory_policy = ModelMemoryPolicy::Balanced;
        }
    }
}

fn move_model_cursor(state: &mut AppState, next: bool) {
    let len = state.model_manager.entries.len();
    if len == 0 {
        return;
    }
    state.model_manager.cursor = if next {
        (state.model_manager.cursor + 1) % len
    } else {
        (state.model_manager.cursor + len - 1) % len
    };
}

fn selected_model(state: &AppState) -> Option<&omarag_app::ModelCatalogEntry> {
    state.model_manager.entries.get(state.model_manager.cursor)
}

fn pull_selected_model(state: &mut AppState) -> Option<UiCommand> {
    if state.model_manager.busy {
        return None;
    }
    let entry = selected_model(state)?.clone();
    if entry.source == ModelSource::Installed {
        notify(
            state,
            NotificationLevel::Info,
            "The selected model is already installed.",
        );
        return None;
    }
    let model = downloadable_model_name(state, &entry);
    state.model_manager.busy = true;
    state.model_manager.transfer_status = format!("Preparing {model}");
    state.model_manager.transfer_completed = 0;
    state.model_manager.transfer_total = 0;
    Some(UiCommand::PullModel { model })
}

fn pull_selected_package(state: &mut AppState) -> Option<UiCommand> {
    if state.model_manager.busy {
        return None;
    }
    let package = state
        .model_manager
        .packages
        .get(state.model_manager.package_cursor)?
        .clone();
    let mut models = Vec::new();
    for model in package.models.iter().filter(|model| !model.installed) {
        if !models.contains(&model.download_name) {
            models.push(model.download_name.clone());
        }
    }
    if models.is_empty() {
        notify(
            state,
            NotificationLevel::Info,
            "Every model in this stack is already installed.",
        );
        return None;
    }
    state.model_manager.busy = true;
    state.model_manager.transfer_status = format!("Preparing {}", package.name);
    Some(UiCommand::PullPackage {
        name: package.name,
        models,
    })
}

fn load_or_pull_selected_model(state: &mut AppState) -> Option<UiCommand> {
    let entry = selected_model(state)?.clone();
    if entry.source != ModelSource::Installed && !entry.installed {
        return pull_selected_model(state);
    }
    if state.model_manager.busy {
        return None;
    }
    state.model_manager.busy = true;
    state.model_manager.transfer_status = format!("Loading {}", entry.id);
    Some(UiCommand::PreloadModel {
        model: entry.id,
        context_tokens: state.model_manager.context_tokens,
        keep_alive: state.model_manager.memory_policy.keep_alive().into(),
    })
}

fn unload_selected_model(state: &mut AppState) -> Option<UiCommand> {
    if state.model_manager.busy {
        return None;
    }
    let model = selected_model(state)?.id.clone();
    state.model_manager.busy = true;
    state.model_manager.transfer_status = format!("Unloading {model}");
    Some(UiCommand::UnloadModel { model })
}

fn request_model_delete(state: &mut AppState) -> Option<UiCommand> {
    if state.model_manager.busy {
        return None;
    }
    let entry = selected_model(state)?.clone();
    if state.model_manager.source != ModelSource::Installed
        || entry.source != ModelSource::Installed
    {
        notify(
            state,
            NotificationLevel::Info,
            "Switch to Installed to delete an exact local model.",
        );
        return None;
    }
    state.model_manager.delete_candidate = Some(entry.id);
    state.overlay = Some(Overlay::ConfirmModelDelete);
    None
}

fn confirm_model_delete(state: &mut AppState) -> Option<UiCommand> {
    let model = state.model_manager.delete_candidate.take()?;
    state.model_manager.busy = true;
    state.model_manager.transfer_status = format!("Deleting {model}");
    state.overlay = Some(Overlay::ModelManager);
    Some(UiCommand::DeleteModel {
        confirm: model.clone(),
        model,
    })
}

fn cancel_model_delete(state: &mut AppState) {
    state.model_manager.delete_candidate = None;
    state.overlay = Some(Overlay::ModelManager);
}

fn downloadable_model_name(state: &AppState, entry: &omarag_app::ModelCatalogEntry) -> String {
    match entry.source {
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
    }
}

fn handle_palette(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    match key.code {
        KeyCode::Esc => {
            update(state, Action::CloseOverlay);
        }
        KeyCode::Up => {
            let count = filtered_palette_commands(state).len();
            if count > 0 {
                state.palette.cursor = (state.palette.cursor + count - 1) % count;
            }
        }
        KeyCode::Down => {
            let count = filtered_palette_commands(state).len();
            if count > 0 {
                state.palette.cursor = (state.palette.cursor + 1) % count;
            }
        }
        KeyCode::Home => state.palette.cursor = 0,
        KeyCode::End => {
            state.palette.cursor = filtered_palette_commands(state).len().saturating_sub(1);
        }
        KeyCode::Backspace => {
            state.palette.query.backspace();
            state.palette.cursor = 0;
        }
        KeyCode::Delete => state.palette.query.delete(),
        KeyCode::Left => state.palette.query.move_left(),
        KeyCode::Right => state.palette.query.move_right(),
        KeyCode::Enter => {
            let command = filtered_palette_commands(state)
                .get(state.palette.cursor)
                .copied();
            if let Some(command) = command {
                return execute_palette(state, command);
            }
        }
        KeyCode::Char(character)
            if !key
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
        {
            state.palette.query.insert_char(character);
            state.palette.cursor = 0;
        }
        _ => {}
    }
    None
}

fn execute_palette(state: &mut AppState, command: PaletteCommand) -> Option<UiCommand> {
    update(state, Action::CloseOverlay);
    let route = match command {
        PaletteCommand::Chat => Some(Route::Chat),
        PaletteCommand::Library => Some(Route::Library),
        PaletteCommand::Sources => Some(Route::Sources),
        PaletteCommand::Jobs => Some(Route::Jobs),
        PaletteCommand::Search => Some(Route::Search),
        PaletteCommand::Quality => Some(Route::Quality),
        PaletteCommand::Backups => Some(Route::Backups),
        PaletteCommand::Settings => Some(Route::Settings),
        PaletteCommand::System => Some(Route::System),
        _ => None,
    };
    if let Some(route) = route {
        update(state, Action::Navigate(route));
        return None;
    }
    match command {
        PaletteCommand::SwitchWorkspace => {
            update(state, Action::OpenOverlay(Overlay::Workspaces));
        }
        PaletteCommand::ToggleLevel => {
            update(state, Action::ToggleInteractionLevel);
        }
        PaletteCommand::RefreshJobs => return Some(UiCommand::RefreshJobs),
        PaletteCommand::RefreshWorkspace => {
            return state
                .active_workspace
                .clone()
                .map(UiCommand::RefreshWorkspaceFeatures);
        }
        PaletteCommand::CreateBackup => {
            if let Some(workspace) = state.active_workspace.clone() {
                update(state, Action::OperationStarted("Creating backup".into()));
                return Some(UiCommand::CreateBackup(workspace));
            }
        }
        PaletteCommand::CancelRun => {
            if let Some(run_id) = state.chat.active_run.clone() {
                update(state, Action::OperationStarted("Stopping answer".into()));
                return Some(UiCommand::CancelRun(run_id));
            }
        }
        PaletteCommand::Help => {
            update(state, Action::OpenOverlay(Overlay::Help));
        }
        _ => {}
    }
    None
}

fn handle_text(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    if key.modifiers.contains(KeyModifiers::CONTROL) {
        if state.route == Route::Settings && matches!(key.code, KeyCode::Char('u' | 'k' | 'w')) {
            state.config_dirty = true;
        }
        match key.code {
            KeyCode::Char('a') => active_editor_mut(state).home(),
            KeyCode::Char('e') => active_editor_mut(state).end(),
            KeyCode::Char('u') => active_editor_mut(state).clear_before(),
            KeyCode::Char('k') => active_editor_mut(state).clear_after(),
            KeyCode::Char('w') => active_editor_mut(state).delete_word(),
            KeyCode::Char('p') => {
                update(state, Action::OpenOverlay(Overlay::Palette));
            }
            KeyCode::Char('s') if state.route == Route::Settings => {
                return save_config(state);
            }
            KeyCode::Home => active_editor_mut(state).home(),
            KeyCode::End => active_editor_mut(state).end(),
            _ => {}
        }
        refresh_path_suggestions(state);
        return None;
    }
    if state.route == Route::Settings
        && matches!(
            key.code,
            KeyCode::Backspace | KeyCode::Delete | KeyCode::Enter | KeyCode::Char(_)
        )
    {
        state.config_dirty = true;
    }
    match key.code {
        KeyCode::Esc => {
            state.library.filtering = false;
            update(state, Action::SetInputMode(InputMode::Nav));
        }
        KeyCode::Up
            if state.route == Route::Library && !state.library.path_suggestions.is_empty() =>
        {
            move_path_suggestion(state, false);
        }
        KeyCode::Down
            if state.route == Route::Library && !state.library.path_suggestions.is_empty() =>
        {
            move_path_suggestion(state, true);
        }
        KeyCode::Up => active_editor_mut(state).move_up(),
        KeyCode::Down => active_editor_mut(state).move_down(),
        KeyCode::Left => active_editor_mut(state).move_left(),
        KeyCode::Right => active_editor_mut(state).move_right(),
        KeyCode::Home => active_editor_mut(state).line_home(),
        KeyCode::End => active_editor_mut(state).line_end(),
        KeyCode::Backspace => active_editor_mut(state).backspace(),
        KeyCode::Delete => active_editor_mut(state).delete(),
        KeyCode::Enter
            if key.modifiers.contains(KeyModifiers::SHIFT) || state.route == Route::Settings =>
        {
            active_editor_mut(state).insert_char('\n');
        }
        KeyCode::Enter => return submit_active_editor(state),
        KeyCode::Char(character) if !key.modifiers.contains(KeyModifiers::ALT) => {
            active_editor_mut(state).insert_char(character);
        }
        _ => {}
    }
    refresh_path_suggestions(state);
    None
}

fn refresh_path_suggestions(state: &mut AppState) {
    if state.route != Route::Library
        || state.input_mode != InputMode::Text
        || state.library.filtering
    {
        state.library.path_suggestions.clear();
        state.library.path_suggestion_cursor = 0;
        return;
    }
    state.library.path_suggestions = complete_paths(&state.library.import_path.value);
    state.library.path_suggestion_cursor = state
        .library
        .path_suggestion_cursor
        .min(state.library.path_suggestions.len().saturating_sub(1));
}

fn move_path_suggestion(state: &mut AppState, next: bool) {
    let len = state.library.path_suggestions.len();
    if len == 0 {
        return;
    }
    state.library.path_suggestion_cursor = if next {
        (state.library.path_suggestion_cursor + 1) % len
    } else {
        (state.library.path_suggestion_cursor + len - 1) % len
    };
}

fn accept_path_suggestion(state: &mut AppState) {
    if state.library.path_suggestions.is_empty() {
        refresh_path_suggestions(state);
    }
    let Some(path) = state
        .library
        .path_suggestions
        .get(state.library.path_suggestion_cursor)
        .cloned()
    else {
        return;
    };
    state.library.import_path.set(path);
    state.library.path_suggestion_cursor = 0;
    refresh_path_suggestions(state);
}

fn complete_paths(input: &str) -> Vec<String> {
    if input.contains('\n') {
        return Vec::new();
    }
    if input == "~" {
        return vec![format!("~{MAIN_SEPARATOR}")];
    }

    let split_at = input
        .rfind(MAIN_SEPARATOR)
        .map_or(0, |index| index + MAIN_SEPARATOR.len_utf8());
    let (display_directory, fragment) = input.split_at(split_at);
    let filesystem_directory = completion_directory(display_directory);
    let Ok(entries) = std::fs::read_dir(filesystem_directory) else {
        return Vec::new();
    };
    let fragment_lower = fragment.to_lowercase();
    let include_hidden = fragment.starts_with('.');
    let mut candidates = entries
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().into_owned();
            if (!include_hidden && name.starts_with('.'))
                || !name.to_lowercase().starts_with(&fragment_lower)
            {
                return None;
            }
            let is_directory = entry.path().is_dir();
            let mut completed = format!("{display_directory}{name}");
            if is_directory {
                completed.push(MAIN_SEPARATOR);
            }
            Some((!is_directory, name.to_lowercase(), completed))
        })
        .collect::<Vec<_>>();
    candidates.sort_by(|left, right| (left.0, &left.1).cmp(&(right.0, &right.1)));
    candidates
        .into_iter()
        .take(8)
        .map(|(_, _, completed)| completed)
        .collect()
}

fn completion_directory(display_directory: &str) -> PathBuf {
    if display_directory == format!("~{MAIN_SEPARATOR}") {
        return std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
    }
    let home_prefix = format!("~{MAIN_SEPARATOR}");
    if let Some(relative) = display_directory.strip_prefix(&home_prefix)
        && let Some(user_home) = std::env::var_os("HOME")
    {
        return Path::new(&user_home).join(relative);
    }
    if display_directory.is_empty() {
        PathBuf::from(".")
    } else {
        PathBuf::from(display_directory)
    }
}

fn submit_active_editor(state: &mut AppState) -> Option<UiCommand> {
    if state.route == Route::Library && state.library.filtering {
        state.library.filtering = false;
        state.input_mode = InputMode::Nav;
        state.asset_cursor = 0;
        return None;
    }
    let Some(workspace) = state.active_workspace.clone() else {
        notify(state, NotificationLevel::Warning, "Select a library first.");
        return None;
    };
    match state.route {
        Route::Chat => {
            let question = state.chat.question.value.trim().to_owned();
            if question.is_empty() {
                notify(state, NotificationLevel::Warning, "Enter a question first.");
                return None;
            }
            if state.chat.active_run.is_some() || state.chat.request_pending {
                notify(
                    state,
                    NotificationLevel::Warning,
                    "The current answer is still running.",
                );
                return None;
            }
            let evidence_mode = state.chat.evidence_mode;
            update(state, Action::RunRequestStarted);
            Some(UiCommand::StartRun {
                workspace,
                question,
                evidence_mode,
            })
        }
        Route::Search => {
            let query = state.search.query.value.trim().to_owned();
            if query.is_empty() {
                notify(
                    state,
                    NotificationLevel::Warning,
                    "Enter a search query first.",
                );
                return None;
            }
            update(state, Action::SearchStarted);
            Some(UiCommand::Search {
                workspace,
                request: SearchRequest::new(query),
            })
        }
        Route::Library => {
            let path = state.library.import_path.value.trim().to_owned();
            if path.is_empty() {
                notify(
                    state,
                    NotificationLevel::Warning,
                    "Enter a file or folder path.",
                );
                return None;
            }
            update(state, Action::ImportStarted);
            Some(UiCommand::Ingest {
                workspace,
                request: IngestRequest::file(path),
            })
        }
        Route::Sources => {
            let location = state.source_location.value.trim().to_owned();
            if location.is_empty() {
                notify(
                    state,
                    NotificationLevel::Warning,
                    "Enter a source path or URL.",
                );
                return None;
            }
            let source_type = if location.starts_with("http://") || location.starts_with("https://")
            {
                "url"
            } else if location.ends_with('/') {
                "directory"
            } else {
                "file"
            };
            let name = location
                .trim_end_matches('/')
                .rsplit('/')
                .next()
                .filter(|value| !value.is_empty())
                .unwrap_or("Source")
                .to_owned();
            update(state, Action::OperationStarted("Saving source".into()));
            Some(UiCommand::CreateSource {
                workspace,
                request: CreateSource {
                    name,
                    source_type: source_type.into(),
                    location,
                    enabled: true,
                },
            })
        }
        _ => {
            update(state, Action::SetInputMode(InputMode::Nav));
            None
        }
    }
}

fn handle_navigation(state: &mut AppState, key: KeyEvent) -> Option<UiCommand> {
    if state.focus == FocusPanel::Activity {
        match key.code {
            KeyCode::Char('r') => return Some(UiCommand::RefreshJobs),
            KeyCode::Char('s') => return cancel_active(state),
            KeyCode::Char('c') => {
                update(state, Action::NotificationDismissed);
                return None;
            }
            _ => {}
        }
    }
    if state.focus == FocusPanel::Sources {
        match key.code {
            KeyCode::Char('/') | KeyCode::Char('s') => {
                state.library.filtering = true;
                state.input_mode = InputMode::Text;
                return None;
            }
            KeyCode::Char('f') => {
                state.library.filter = state.library.filter.next();
                state.asset_cursor = 0;
                return None;
            }
            KeyCode::Char('o') => {
                state.library.sort = state.library.sort.next();
                state.asset_cursor = 0;
                return None;
            }
            KeyCode::Char('i') => {
                if selected_library_document(state).is_some() {
                    state.overlay = Some(Overlay::DocumentDetails);
                }
                return None;
            }
            KeyCode::Char('t') => {
                if let Some(document) = selected_library_document(state) {
                    let tags = state
                        .document_tags
                        .get(&document.id)
                        .cloned()
                        .unwrap_or_default()
                        .join(", ");
                    state.tag_editor.set(tags);
                    state.overlay = Some(Overlay::DocumentTags);
                }
                return None;
            }
            KeyCode::Delete | KeyCode::Char('d') => {
                if selected_library_job(state).is_some() {
                    hide_selected_library_job(state);
                } else if selected_library_document(state).is_some() {
                    state.overlay = Some(Overlay::ConfirmDocumentDelete);
                }
                return None;
            }
            KeyCode::Char('r') => return retry_selected_library_job(state),
            KeyCode::Char(' ') | KeyCode::Char('u') => return toggle_selected_library_job(state),
            KeyCode::Char('v') => return activate_selection(state),
            KeyCode::Char('x') => return cancel_selected_library_job(state),
            KeyCode::Char('p') => {
                state.profile_cursor = state.active_profile_index();
                state.overlay = Some(Overlay::WorkspaceProfile);
                return None;
            }
            KeyCode::Char('l') => {
                state.creating_workspace = false;
                state.overlay = Some(Overlay::Workspaces);
                return None;
            }
            _ => {}
        }
    }
    if state.focus == FocusPanel::Chat {
        match key.code {
            KeyCode::Char('[') => {
                move_citation(state, false);
                return None;
            }
            KeyCode::Char(']') => {
                move_citation(state, true);
                return None;
            }
            KeyCode::Char('o') => return selected_citation_command(state, false),
            KeyCode::Char('v') => return selected_citation_command(state, true),
            KeyCode::Char('c') => return selected_citation_copy(state),
            KeyCode::Char('h') => {
                state.overlay = Some(Overlay::ChatHistory);
                return None;
            }
            KeyCode::Char('r') => return repeat_current_question(state),
            KeyCode::Char('e') => {
                state.input_mode = InputMode::Text;
                return None;
            }
            KeyCode::Char('x') => return export_current_chat(state),
            _ => {}
        }
    }
    match key.code {
        KeyCode::Char(':') => {
            update(state, Action::OpenOverlay(Overlay::Palette));
        }
        KeyCode::Char('?') => {
            update(state, Action::OpenOverlay(Overlay::Help));
        }
        KeyCode::Left | KeyCode::Char('h') => {
            update(state, Action::FocusPrevious);
        }
        KeyCode::Right | KeyCode::Char('l') => {
            update(state, Action::FocusNext);
        }
        KeyCode::Up | KeyCode::Char('k') => move_selection(state, false),
        KeyCode::Down | KeyCode::Char('j') => move_selection(state, true),
        KeyCode::PageUp => move_selection_many(state, false, 5),
        KeyCode::PageDown => move_selection_many(state, true, 5),
        KeyCode::Enter => return activate_selection(state),
        KeyCode::Char('i') => open_file_browser(state),
        KeyCode::Char('n') => start_workspace_creation(state),
        KeyCode::Char('w') => {
            state.creating_workspace = false;
            state.overlay = Some(Overlay::Workspaces);
        }
        KeyCode::Char(' ') => return toggle_selected_job(state),
        KeyCode::Char('x') => return cancel_active(state),
        _ => {}
    }
    None
}

fn move_selection(state: &mut AppState, next: bool) {
    match state.focus {
        FocusPanel::Navigation => {
            const LEN: usize = 9;
            state.nav_cursor = if next {
                (state.nav_cursor + 1) % LEN
            } else {
                (state.nav_cursor + LEN - 1) % LEN
            };
        }
        FocusPanel::Chat => {
            state.chat_scroll = if next {
                state.chat_scroll.saturating_add(1)
            } else {
                state.chat_scroll.saturating_sub(1)
            };
        }
        FocusPanel::Import => {}
        FocusPanel::Sources => {
            let len = visible_library_jobs(state).len() + visible_document_indices(state).len();
            if len > 0 {
                state.asset_cursor = if next {
                    (state.asset_cursor + 1) % len
                } else {
                    (state.asset_cursor + len - 1) % len
                };
                sync_document_cursor(state);
            }
        }
        FocusPanel::Hardware => {
            state.hardware_cursor = if next {
                (state.hardware_cursor + 1) % 5
            } else {
                (state.hardware_cursor + 4) % 5
            };
        }
        FocusPanel::Models => {
            state.model_cursor = if next {
                (state.model_cursor + 1) % 4
            } else {
                (state.model_cursor + 3) % 4
            };
        }
        FocusPanel::Activity => {
            update(
                state,
                if next {
                    Action::SelectNextJob
                } else {
                    Action::SelectPreviousJob
                },
            );
        }
    }
}

fn move_selection_many(state: &mut AppState, next: bool, count: usize) {
    for _ in 0..count {
        move_selection(state, next);
    }
}

fn activate_selection(state: &mut AppState) -> Option<UiCommand> {
    match state.focus {
        FocusPanel::Navigation => activate_navigation(state),
        FocusPanel::Chat => {
            update(state, Action::Navigate(Route::Chat));
            update(state, Action::SetInputMode(InputMode::Text));
            None
        }
        FocusPanel::Import => {
            open_file_browser(state);
            None
        }
        FocusPanel::Sources => {
            if selected_library_job(state).is_some() {
                return toggle_selected_library_job(state);
            }
            if let Some(document) = selected_library_document(state) {
                Some(UiCommand::OpenPdf {
                    path: document.source.clone(),
                    page: None,
                })
            } else {
                state.creating_workspace = false;
                state.overlay = Some(Overlay::Workspaces);
                None
            }
        }
        FocusPanel::Models => open_model_manager(state),
        FocusPanel::Activity => toggle_selected_job(state),
        _ => None,
    }
}

fn open_model_manager(state: &mut AppState) -> Option<UiCommand> {
    update(state, Action::OpenOverlay(Overlay::ModelManager));
    Some(refresh_model_catalog_command(state))
}

fn activate_navigation(state: &mut AppState) -> Option<UiCommand> {
    let code = match state.nav_cursor {
        0 => KeyCode::Char('c'),
        1 => KeyCode::Char('s'),
        2 => KeyCode::Char('h'),
        3 => KeyCode::Char('m'),
        4 => KeyCode::Char('a'),
        5 => KeyCode::Char('t'),
        6 => KeyCode::Char('w'),
        7 => KeyCode::Char('p'),
        _ => KeyCode::Char('q'),
    };
    handle_ctrl_shortcut(state, code).flatten()
}

fn toggle_selected_job(state: &mut AppState) -> Option<UiCommand> {
    if state.focus != FocusPanel::Activity && state.route != Route::Jobs {
        return None;
    }
    let (id, status) = selected_job(state)?;
    let command = match status {
        JobStatus::Paused => JobCommand::Resume,
        JobStatus::Queued | JobStatus::Running | JobStatus::PauseRequested => JobCommand::Pause,
        JobStatus::Completed | JobStatus::Cancelled | JobStatus::Failed => return None,
    };
    update(state, Action::OperationStarted("Updating job".into()));
    Some(UiCommand::Job { id, command })
}

fn cancel_active(state: &mut AppState) -> Option<UiCommand> {
    if let Some(run_id) = state.chat.active_run.clone() {
        update(state, Action::OperationStarted("Stopping answer".into()));
        return Some(UiCommand::CancelRun(run_id));
    }
    let (id, status) = selected_job(state)?;
    if matches!(
        status,
        JobStatus::Completed | JobStatus::Cancelled | JobStatus::Failed
    ) {
        return None;
    }
    if let Some(job) = state.jobs.get(&id).cloned()
        && job.kind == "ingest"
    {
        state.undo = Some(UndoAction::CancelledJob(job));
    }
    update(state, Action::OperationStarted("Stopping job".into()));
    Some(UiCommand::Job {
        id,
        command: JobCommand::Cancel,
    })
}

fn selected_workspace(state: &AppState) -> Option<WorkspaceId> {
    state
        .workspaces
        .get(state.workspace_cursor)
        .map(|workspace| workspace.id.clone())
}

fn selected_job(state: &AppState) -> Option<(JobId, JobStatus)> {
    state
        .jobs
        .values()
        .nth(state.job_cursor)
        .map(|job| (job.id.clone(), job.status.clone()))
}

fn active_editor_mut(state: &mut AppState) -> &mut EditorState {
    match state.route {
        Route::Search => &mut state.search.query,
        Route::Library if state.library.filtering => &mut state.library.query,
        Route::Library => &mut state.library.import_path,
        Route::Sources => &mut state.source_location,
        Route::Settings => &mut state.config_editor,
        _ => &mut state.chat.question,
    }
}

fn save_config(state: &mut AppState) -> Option<UiCommand> {
    let workspace = state.active_workspace.clone()?;
    let config = state.config.as_ref()?;
    if !state.config_dirty {
        notify(
            state,
            NotificationLevel::Info,
            "The configuration has not changed.",
        );
        return None;
    }
    let content = state.config_editor.value.clone();
    if content.trim().is_empty() {
        notify(
            state,
            NotificationLevel::Warning,
            "The configuration cannot be empty.",
        );
        return None;
    }
    let etag = config.etag.clone();
    update(
        state,
        Action::OperationStarted("Saving configuration".into()),
    );
    Some(UiCommand::SaveConfig {
        workspace,
        request: UpdateConfig { content },
        etag,
    })
}

fn cycle_evidence_mode(state: &mut AppState) {
    state.chat.evidence_mode = match state.chat.evidence_mode {
        EvidenceMode::Strict => EvidenceMode::Normal,
        EvidenceMode::Normal => EvidenceMode::Explore,
        EvidenceMode::Explore => EvidenceMode::Strict,
    };
}

fn notify(state: &mut AppState, level: NotificationLevel, message: &str) {
    update(
        state,
        Action::Notify(Notification {
            level,
            message: message.into(),
        }),
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::{KeyEvent, MouseEvent};
    use omarag_app::{FileBrowserEntry, InteractionLevel};
    use omarag_domain::{ConfigDocument, WorkspaceSummary};

    fn key(code: KeyCode) -> Event {
        Event::Key(KeyEvent::new(code, KeyModifiers::NONE))
    }

    fn modified_key(code: KeyCode, modifiers: KeyModifiers) -> Event {
        Event::Key(KeyEvent::new(code, modifiers))
    }

    fn mouse(kind: MouseEventKind, column: u16, row: u16) -> MouseEvent {
        MouseEvent {
            kind,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        }
    }

    #[test]
    fn palette_filters_and_executes_selection() {
        let mut state = AppState::default();
        handle_event(&mut state, key(KeyCode::Char(':')));
        assert_eq!(state.overlay, Some(Overlay::Palette));
        for character in "search".chars() {
            handle_event(&mut state, key(KeyCode::Char(character)));
        }
        assert_eq!(
            filtered_palette_commands(&state).first(),
            Some(&PaletteCommand::Search)
        );
        handle_event(&mut state, key(KeyCode::Enter));
        assert_eq!(state.route, Route::Search);
        assert_eq!(state.overlay, None);
    }

    #[test]
    fn nucleo_fuzzy_search_handles_sparse_terms() {
        assert!(fuzzy_score("Hugging Face Models", "hfm").is_some());
        assert!(fuzzy_score("Hugging Face Models", "zzq").is_none());
    }

    #[test]
    fn text_editor_supports_cursor_delete_and_paste() {
        let mut state = AppState {
            input_mode: InputMode::Text,
            ..AppState::default()
        };
        handle_event(&mut state, Event::Paste("Hllo".into()));
        handle_event(&mut state, key(KeyCode::Home));
        handle_event(&mut state, key(KeyCode::Right));
        handle_event(&mut state, key(KeyCode::Char('a')));
        handle_event(&mut state, key(KeyCode::Delete));
        assert_eq!(state.chat.question.value, "Halo");
    }

    #[test]
    fn file_browser_multiselect_confirms_one_ingest_request() {
        let root =
            std::env::temp_dir().join(format!("oracle-import-test-{}", uuid::Uuid::new_v4()));
        let folder = root.join("specs");
        std::fs::create_dir_all(&folder).unwrap();
        std::fs::write(root.join("a.pdf"), b"pdf").unwrap();
        std::fs::write(folder.join("b.pdf"), b"pdf").unwrap();
        let mut state = AppState {
            overlay: Some(Overlay::FileBrowser),
            active_workspace: Some("ws-test".into()),
            ..AppState::default()
        };
        state.file_browser.entries = vec![
            FileBrowserEntry {
                path: root.join("a.pdf").to_string_lossy().into_owned(),
                name: "a.pdf".into(),
                is_dir: false,
            },
            FileBrowserEntry {
                path: folder.to_string_lossy().into_owned(),
                name: "specs".into(),
                is_dir: true,
            },
        ];

        handle_event(&mut state, key(KeyCode::Char(' ')));
        handle_event(&mut state, key(KeyCode::Down));
        handle_event(&mut state, key(KeyCode::Char(' ')));
        let analyze = handle_event(&mut state, key(KeyCode::Enter));
        assert_eq!(state.overlay, Some(Overlay::ConfirmImport));
        assert!(matches!(analyze, Some(UiCommand::AnalyzeImport { .. })));
        state.library.preflight = ImportPreflight {
            pdfs: vec![
                root.join("a.pdf").to_string_lossy().into_owned(),
                folder.join("b.pdf").to_string_lossy().into_owned(),
            ],
            ..ImportPreflight::default()
        };

        let command = handle_event(&mut state, key(KeyCode::Enter));
        assert!(matches!(
            command,
            Some(UiCommand::Ingest { request, .. }) if request.sources.len() == 2
        ));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn repeat_events_are_accepted_and_release_is_ignored() {
        let mut state = AppState {
            input_mode: InputMode::Text,
            ..AppState::default()
        };
        let repeat =
            KeyEvent::new_with_kind(KeyCode::Char('x'), KeyModifiers::NONE, KeyEventKind::Repeat);
        let release = KeyEvent::new_with_kind(
            KeyCode::Char('y'),
            KeyModifiers::NONE,
            KeyEventKind::Release,
        );
        handle_event(&mut state, Event::Key(repeat));
        handle_event(&mut state, Event::Key(release));
        assert_eq!(state.chat.question.value, "x");
    }

    #[test]
    fn tab_moves_focus_instead_of_changing_level() {
        let mut state = AppState::default();
        handle_event(&mut state, key(KeyCode::Tab));
        assert_eq!(state.focus, FocusPanel::Import);
        assert_eq!(state.interaction_level, InteractionLevel::Simple);
    }

    #[test]
    fn arrows_tab_and_ctrl_shortcuts_cover_all_panels() {
        let mut state = AppState::default();
        handle_event(&mut state, key(KeyCode::Down));
        assert_eq!(state.chat_scroll, 1);
        handle_event(&mut state, key(KeyCode::Char('k')));
        assert_eq!(state.chat_scroll, 0);
        handle_event(&mut state, key(KeyCode::Left));
        assert_eq!(state.focus, FocusPanel::Activity);
        handle_event(
            &mut state,
            modified_key(KeyCode::Char('t'), KeyModifiers::CONTROL),
        );
        assert_eq!(state.theme_index, 1);
        handle_event(&mut state, key(KeyCode::Tab));
        assert_eq!(state.focus, FocusPanel::Chat);
        handle_event(
            &mut state,
            modified_key(KeyCode::Char('h'), KeyModifiers::CONTROL),
        );
        assert_eq!(state.focus, FocusPanel::Models);
        handle_event(&mut state, key(KeyCode::Down));
        assert_eq!(state.model_cursor, 1);
        handle_event(
            &mut state,
            modified_key(KeyCode::Char('m'), KeyModifiers::CONTROL),
        );
        assert_eq!(state.focus, FocusPanel::Models);
        handle_event(
            &mut state,
            modified_key(KeyCode::Char('a'), KeyModifiers::CONTROL),
        );
        assert_eq!(state.focus, FocusPanel::Activity);
    }

    #[test]
    fn mouse_clicks_focus_and_activate_dashboard_controls() {
        let screen = Rect::new(0, 0, 160, 40);
        let [header, body, _footer] = screen_areas(screen);
        let [chat, _library, compute, _activity] = dashboard_areas(body);
        let mut state = AppState::default();

        handle_mouse(
            &mut state,
            mouse(
                MouseEventKind::Down(MouseButton::Left),
                chat.x + 3,
                chat.bottom() - 2,
            ),
            screen,
        );
        assert_eq!(state.focus, FocusPanel::Chat);
        assert_eq!(state.input_mode, InputMode::Text);

        handle_mouse(
            &mut state,
            mouse(
                MouseEventKind::Down(MouseButton::Left),
                compute.right() - 4,
                compute.y + 2,
            ),
            screen,
        );
        assert_eq!(state.focus, FocusPanel::Models);

        handle_mouse(
            &mut state,
            mouse(
                MouseEventKind::Down(MouseButton::Left),
                header_import_area(header).x + 2,
                header_import_area(header).y + 1,
            ),
            screen,
        );
        assert_eq!(state.focus, FocusPanel::Import);
        assert_eq!(state.overlay, Some(Overlay::FileBrowser));
    }

    #[test]
    fn mouse_wheel_and_middle_button_cover_chat_and_themes() {
        let screen = Rect::new(0, 0, 160, 40);
        let [_header, body, _footer] = screen_areas(screen);
        let [chat, ..] = dashboard_areas(body);
        let mut state = AppState::default();

        handle_mouse(
            &mut state,
            mouse(MouseEventKind::ScrollDown, chat.x + 2, chat.y + 2),
            screen,
        );
        assert_eq!(state.chat_scroll, 1);
        handle_mouse(
            &mut state,
            mouse(MouseEventKind::Down(MouseButton::Middle), 0, 0),
            screen,
        );
        assert_eq!(state.theme_index, 1);
    }

    #[test]
    fn import_path_completion_lists_directories_and_tab_accepts() {
        let test_root =
            std::env::temp_dir().join(format!("omarag-path-completion-{}", std::process::id()));
        let documents = test_root.join("Documents");
        let downloads = test_root.join("Downloads");
        std::fs::create_dir_all(&documents).unwrap();
        std::fs::create_dir_all(&downloads).unwrap();

        let prefix = format!("{}{MAIN_SEPARATOR}Doc", test_root.display());
        let suggestions = complete_paths(&prefix);
        assert_eq!(
            suggestions,
            vec![format!("{}{MAIN_SEPARATOR}", documents.display())]
        );

        let mut state = AppState {
            route: Route::Library,
            focus: FocusPanel::Sources,
            input_mode: InputMode::Text,
            ..AppState::default()
        };
        state.library.import_path.set(prefix);
        refresh_path_suggestions(&mut state);
        handle_event(&mut state, key(KeyCode::Tab));
        assert_eq!(
            state.library.import_path.value,
            format!("{}{MAIN_SEPARATOR}", documents.display())
        );

        std::fs::remove_dir_all(&test_root).unwrap();
    }

    #[test]
    fn model_manager_opens_and_builds_quantized_download_commands() {
        let mut state = AppState {
            focus: FocusPanel::Models,
            ..AppState::default()
        };
        let command = handle_event(&mut state, key(KeyCode::Enter));
        assert_eq!(state.overlay, Some(Overlay::ModelManager));
        assert!(matches!(
            command,
            Some(UiCommand::RefreshModelCatalog {
                source: ModelSource::Installed,
                ..
            })
        ));

        state.model_manager.source = ModelSource::Ollama;
        state.model_manager.busy = false;
        state.model_manager.entries = vec![omarag_app::ModelCatalogEntry {
            id: "qwen3.5:2b".into(),
            source: ModelSource::Ollama,
            description: "Small Qwen".into(),
            parameter_count: Some(2_000_000_000),
            ..omarag_app::ModelCatalogEntry::default()
        }];
        let command = handle_event(&mut state, key(KeyCode::Char('d')));
        assert!(matches!(
            command,
            Some(UiCommand::PullModel { model }) if model == "qwen3.5:2b-q4_K_M"
        ));
    }

    #[test]
    fn model_manager_tabs_support_mouse_and_shift_arrows() {
        let screen = Rect::new(0, 0, 160, 40);
        let [_area, sidebar, _search, _list, _details, _footer] = model_manager_areas(screen);
        let mut state = AppState {
            overlay: Some(Overlay::ModelManager),
            ..AppState::default()
        };
        state.model_manager.packages = (1..=3)
            .map(|rank| omarag_app::ModelPackage {
                id: format!("stack-{rank}"),
                name: format!("S{rank}"),
                recommended_rank: rank,
                ..omarag_app::ModelPackage::default()
            })
            .collect();

        // Providers, roles and stacks are direct rows in the left navigation.
        let command = handle_mouse(
            &mut state,
            mouse(
                MouseEventKind::Down(MouseButton::Left),
                sidebar.x + 3,
                sidebar.y + 2,
            ),
            screen,
        );
        assert_eq!(state.model_manager.source, ModelSource::Ollama);
        assert!(matches!(
            command,
            Some(UiCommand::RefreshModelCatalog { .. })
        ));

        handle_mouse(
            &mut state,
            mouse(
                MouseEventKind::Down(MouseButton::Left),
                sidebar.x + 3,
                sidebar.y + 13,
            ),
            screen,
        );
        assert_eq!(state.model_manager.package_cursor, 1);
        handle_event(&mut state, key(KeyCode::Char('3')));
        assert_eq!(state.model_manager.package_cursor, 2);

        let command = handle_mouse(
            &mut state,
            mouse(
                MouseEventKind::Down(MouseButton::Left),
                sidebar.x + 3,
                sidebar.y + 8,
            ),
            screen,
        );
        assert_eq!(state.model_manager.category, ModelCategory::Embedding);
        assert!(matches!(
            command,
            Some(UiCommand::RefreshModelCatalog { .. })
        ));

        handle_event(
            &mut state,
            modified_key(KeyCode::Right, KeyModifiers::SHIFT),
        );
        assert_eq!(state.model_manager.category, ModelCategory::Rerank);
        handle_event(&mut state, modified_key(KeyCode::Left, KeyModifiers::SHIFT));
        assert_eq!(state.model_manager.category, ModelCategory::Embedding);
    }

    #[test]
    fn model_manager_preload_uses_context_and_temporary_residency() {
        let mut state = AppState {
            overlay: Some(Overlay::ModelManager),
            ..AppState::default()
        };
        state.model_manager.entries = vec![omarag_app::ModelCatalogEntry {
            id: "qwen3.5:2b-q4_K_M".into(),
            source: ModelSource::Installed,
            installed: true,
            ..omarag_app::ModelCatalogEntry::default()
        }];
        let command = handle_event(&mut state, key(KeyCode::Char('l')));
        assert!(matches!(
            command,
            Some(UiCommand::PreloadModel {
                model,
                context_tokens: 8_192,
                keep_alive,
            }) if model == "qwen3.5:2b-q4_K_M" && keep_alive == "5m"
        ));
    }

    #[test]
    fn model_delete_requires_confirmation_and_returns_to_manager() {
        let mut state = AppState {
            overlay: Some(Overlay::ModelManager),
            ..AppState::default()
        };
        state.model_manager.entries = vec![omarag_app::ModelCatalogEntry {
            id: "local/model:2b".into(),
            source: ModelSource::Installed,
            installed: true,
            ..omarag_app::ModelCatalogEntry::default()
        }];

        assert!(handle_event(&mut state, key(KeyCode::Char('x'))).is_none());
        assert_eq!(state.overlay, Some(Overlay::ConfirmModelDelete));
        assert_eq!(
            state.model_manager.delete_candidate.as_deref(),
            Some("local/model:2b")
        );

        handle_event(&mut state, key(KeyCode::Esc));
        assert_eq!(state.overlay, Some(Overlay::ModelManager));
        assert!(state.model_manager.delete_candidate.is_none());

        handle_event(&mut state, key(KeyCode::Delete));
        let command = handle_event(&mut state, key(KeyCode::Enter));
        assert!(matches!(
            command,
            Some(UiCommand::DeleteModel { model, confirm })
                if model == "local/model:2b" && confirm == model
        ));
        assert_eq!(state.overlay, Some(Overlay::ModelManager));
        assert!(state.model_manager.busy);
    }

    #[test]
    fn model_delete_confirmation_supports_mouse() {
        let screen = Rect::new(0, 0, 160, 40);
        let area = delete_model_confirm_area(screen);
        let mut state = AppState {
            overlay: Some(Overlay::ConfirmModelDelete),
            ..AppState::default()
        };
        state.model_manager.delete_candidate = Some("local/model:2b".into());

        let command = handle_mouse(
            &mut state,
            mouse(
                MouseEventKind::Down(MouseButton::Left),
                area.x + 3,
                area.bottom() - 2,
            ),
            screen,
        );
        assert!(matches!(command, Some(UiCommand::DeleteModel { .. })));
        assert_eq!(state.overlay, Some(Overlay::ModelManager));
    }

    #[test]
    fn model_stack_install_deduplicates_shared_chat_and_vl_model() {
        let mut state = AppState {
            overlay: Some(Overlay::ModelManager),
            ..AppState::default()
        };
        state.model_manager.packages.push(omarag_app::ModelPackage {
            name: "Qwen Unified".into(),
            models: vec![
                omarag_app::ModelPackageItem {
                    role: ModelCategory::Chat,
                    model: "qwen3.5:2b".into(),
                    download_name: "qwen3.5:2b".into(),
                    source: ModelSource::Ollama,
                    installed: false,
                },
                omarag_app::ModelPackageItem {
                    role: ModelCategory::Vl,
                    model: "qwen3.5:2b".into(),
                    download_name: "qwen3.5:2b".into(),
                    source: ModelSource::Ollama,
                    installed: false,
                },
                omarag_app::ModelPackageItem {
                    role: ModelCategory::Embedding,
                    model: "qwen3-embedding:0.6b".into(),
                    download_name: "qwen3-embedding:0.6b".into(),
                    source: ModelSource::Ollama,
                    installed: false,
                },
            ],
            ..omarag_app::ModelPackage::default()
        });
        let command = handle_event(&mut state, key(KeyCode::Char('a')));
        assert!(matches!(
            command,
            Some(UiCommand::PullPackage { name, models })
                if name == "Qwen Unified"
                    && models == vec!["qwen3.5:2b", "qwen3-embedding:0.6b"]
        ));
    }

    #[test]
    fn source_input_creates_a_typed_request() {
        let mut state = AppState {
            route: Route::Sources,
            input_mode: InputMode::Text,
            active_workspace: Some("ws-test".into()),
            ..AppState::default()
        };
        handle_event(&mut state, Event::Paste("https://example.test/docs".into()));
        let command = handle_event(&mut state, key(KeyCode::Enter));
        assert!(matches!(
            command,
            Some(UiCommand::CreateSource { request, .. })
                if request.source_type == "url" && request.name == "docs"
        ));
    }

    #[test]
    fn raw_config_is_saved_with_etag() {
        let mut state = AppState {
            route: Route::Settings,
            input_mode: InputMode::Text,
            active_workspace: Some("ws-test".into()),
            config: Some(ConfigDocument {
                content: "embeddings:\nqa:\n".into(),
                etag: "etag-1".into(),
            }),
            ..AppState::default()
        };
        state.config_editor.set("embeddings:\nqa:\n");
        handle_event(&mut state, key(KeyCode::Char('#')));
        let command = handle_event(
            &mut state,
            modified_key(KeyCode::Enter, KeyModifiers::CONTROL),
        );
        assert!(matches!(
            command,
            Some(UiCommand::SaveConfig { etag, .. }) if etag == "etag-1"
        ));
    }

    #[test]
    fn workspace_profile_is_applied_and_undoable() {
        let mut state = AppState {
            active_workspace: Some("ws-test".into()),
            overlay: Some(Overlay::WorkspaceProfile),
            profile_cursor: 3,
            ..AppState::default()
        };
        handle_event(&mut state, key(KeyCode::Enter));
        assert_eq!(state.active_profile(), WorkspaceProfile::LowMemory);
        handle_event(
            &mut state,
            modified_key(KeyCode::Char('z'), KeyModifiers::CONTROL),
        );
        assert_eq!(state.active_profile(), WorkspaceProfile::Technical);
    }

    #[test]
    fn new_library_name_accepts_profile_shortcut_letters() {
        let mut state = AppState {
            overlay: Some(Overlay::Workspaces),
            creating_workspace: true,
            ..AppState::default()
        };
        for character in "compact".chars() {
            handle_event(&mut state, key(KeyCode::Char(character)));
        }
        assert_eq!(state.workspace_name.value, "compact");

        handle_event(
            &mut state,
            modified_key(KeyCode::Char('p'), KeyModifiers::ALT),
        );
        assert_eq!(state.overlay, Some(Overlay::WorkspaceProfile));
    }

    #[test]
    fn custom_library_profile_controls_the_import_request() {
        let library = "library-test".to_string();
        let mut state = AppState {
            active_workspace: Some(library.clone()),
            overlay: Some(Overlay::WorkspaceProfile),
            ..AppState::default()
        };
        handle_event(&mut state, key(KeyCode::Char('c')));
        assert_eq!(state.overlay, Some(Overlay::CustomProfileEditor));
        handle_event(&mut state, key(KeyCode::Tab));
        handle_event(&mut state, key(KeyCode::Right));
        handle_event(&mut state, key(KeyCode::Tab));
        handle_event(&mut state, key(KeyCode::Right));
        handle_event(&mut state, key(KeyCode::Tab));
        handle_event(&mut state, key(KeyCode::Right));
        handle_event(
            &mut state,
            modified_key(KeyCode::Char('s'), KeyModifiers::CONTROL),
        );
        assert_eq!(state.custom_profiles.len(), 1);
        assert_eq!(state.overlay, Some(Overlay::WorkspaceProfile));
        handle_event(&mut state, key(KeyCode::Enter));

        state.library.preflight.pdfs = vec!["/tmp/manual.pdf".into()];
        state.file_browser.selected = vec!["/tmp/manual.pdf".into()];
        state.overlay = Some(Overlay::ConfirmImport);
        let command = handle_event(&mut state, key(KeyCode::Enter));
        assert!(matches!(
            command,
            Some(UiCommand::Ingest { request, .. })
                if request.processing_profile == "technical"
                    && request.duplicate_policy == "skip"
                    && request.validity_policy == "strict"
        ));
    }

    #[test]
    fn library_delete_requires_confirmation_and_supports_safe_or_physical_mode() {
        let library = WorkspaceSummary {
            id: "library-test".into(),
            name: "Manuals".into(),
            path: "/tmp/manuals.omarag".into(),
            read_only: false,
            updated_at: "now".into(),
            etag: "etag".into(),
        };
        let mut safe = AppState {
            workspaces: vec![library.clone()],
            overlay: Some(Overlay::ConfirmLibraryDelete),
            ..AppState::default()
        };
        assert!(matches!(
            handle_event(&mut safe, key(KeyCode::Enter)),
            Some(UiCommand::DeleteLibrary {
                physical: false,
                ..
            })
        ));

        let mut physical = AppState {
            workspaces: vec![library],
            overlay: Some(Overlay::ConfirmLibraryDelete),
            ..AppState::default()
        };
        assert!(matches!(
            handle_event(&mut physical, key(KeyCode::Char('D'))),
            Some(UiCommand::DeleteLibrary { physical: true, .. })
        ));
    }

    #[test]
    fn library_filter_uses_slash_and_persists_query() {
        let mut state = AppState {
            focus: FocusPanel::Sources,
            route: Route::Library,
            ..AppState::default()
        };
        handle_event(&mut state, key(KeyCode::Char('/')));
        handle_event(&mut state, Event::Paste("concrete".into()));
        handle_event(&mut state, key(KeyCode::Enter));
        assert_eq!(state.library.query.value, "concrete");
        assert!(!state.library.filtering);
        assert_eq!(state.input_mode, InputMode::Nav);
    }

    #[test]
    fn chat_history_can_restore_a_session() {
        let workspace = "ws-test".to_string();
        let mut state = AppState {
            active_workspace: Some(workspace.clone()),
            overlay: Some(Overlay::ChatHistory),
            ..AppState::default()
        };
        state.chat_sessions.insert(
            workspace.clone(),
            vec![ChatSession {
                workspace_id: workspace,
                question: "Question".into(),
                answer: "Answer".into(),
                citations: Vec::new(),
                created_at: "now".into(),
            }],
        );
        handle_event(&mut state, key(KeyCode::Enter));
        assert_eq!(state.chat.question.value, "Question");
        assert_eq!(state.chat.answer, "Answer");
        assert_eq!(state.overlay, None);
    }
}
