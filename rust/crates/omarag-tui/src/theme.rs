//! Colour themes.
//!
//! A theme carries two layers. The **base** layer is the flat palette every
//! widget can fall back to. The **region** layer gives the sidebar, workspace,
//! footer and modals their own border and selection colours — that separation is
//! what makes a superfile-style interface read as distinct panels rather than
//! one wall of accent.
//!
//! Themes are TOML, normalised from upstream projects by
//! `scripts/import_themes.py`. The bundled set is compiled in (see `build.rs`);
//! anything in `~/.config/omarag/themes/` is loaded on top and may override a
//! bundled theme of the same name.

use ratatui::style::Color;
use serde::Deserialize;
use std::{
    fs,
    path::PathBuf,
    sync::{OnceLock, RwLock},
};

include!(concat!(env!("OUT_DIR"), "/themes.rs"));

/// Light or dark ground. Drives contrast decisions that cannot be read off a
/// single colour, and lets the picker group sensibly.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum ThemeMode {
    #[default]
    Dark,
    Light,
}

/// Where a theme came from. Shown in the picker so two schemes with the same
/// family name stay tellable apart.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum ThemeSource {
    #[default]
    Bundled,
    Omarchy,
    Superfile,
    /// Live palette read from the running Omarchy desktop.
    System,
    /// Dropped into the user's theme directory.
    User,
}

impl ThemeSource {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Bundled => "built-in",
            Self::Omarchy => "Omarchy",
            Self::Superfile => "superfile",
            Self::System => "system",
            Self::User => "yours",
        }
    }
}

/// Border and selection colours for one region of the shell.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Region {
    pub fg: Color,
    pub bg: Color,
    pub border: Color,
    pub border_active: Color,
    pub title: Color,
    pub item_selected_fg: Color,
    pub item_selected_bg: Color,
}

/// Modal surfaces, including the two decision buttons.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Modal {
    pub fg: Color,
    pub bg: Color,
    pub border_active: Color,
    pub cancel_fg: Color,
    pub cancel_bg: Color,
    pub confirm_fg: Color,
    pub confirm_bg: Color,
}

/// Colours that carry a fixed meaning rather than a place.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StatusColors {
    pub cursor: Color,
    pub correct: Color,
    pub error: Color,
    pub hint: Color,
    pub cancel: Color,
    pub hotkey: Color,
}

// ----------------------------------------------------------------- TOML shape

#[derive(Debug, Deserialize)]
struct ThemeFile {
    name: String,
    #[serde(default)]
    source: String,
    #[serde(default)]
    mode: String,
    base: BaseTable,
    sidebar: RegionTable,
    workspace: RegionTable,
    footer: FooterTable,
    modal: ModalTable,
    status: StatusTable,
    ansi: AnsiTable,
}

#[derive(Debug, Deserialize)]
struct BaseTable {
    background: String,
    foreground: String,
    muted: String,
    accent: String,
    selection: String,
    #[serde(default)]
    gradient: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct RegionTable {
    fg: String,
    bg: String,
    border: String,
    border_active: String,
    title: String,
    item_selected_fg: String,
    item_selected_bg: String,
}

#[derive(Debug, Deserialize)]
struct FooterTable {
    fg: String,
    bg: String,
    border: String,
    border_active: String,
}

#[derive(Debug, Deserialize)]
struct ModalTable {
    fg: String,
    bg: String,
    border_active: String,
    cancel_fg: String,
    cancel_bg: String,
    confirm_fg: String,
    confirm_bg: String,
}

#[derive(Debug, Deserialize)]
struct StatusTable {
    cursor: String,
    correct: String,
    error: String,
    hint: String,
    cancel: String,
    #[serde(default)]
    hotkey: String,
}

#[derive(Debug, Deserialize)]
struct AnsiTable {
    red: String,
    orange: String,
    yellow: String,
    green: String,
    cyan: String,
    #[serde(default)]
    blue: String,
    magenta: String,
}

// ----------------------------------------------------------------- parsing

pub(crate) fn parse_hex(value: &str) -> Option<Color> {
    let text = value.trim().trim_matches('"');
    let digits = text
        .strip_prefix('#')
        .or_else(|| text.strip_prefix("0x"))
        .unwrap_or(text);
    if digits.len() != 6 || !digits.chars().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }
    let value = u32::from_str_radix(digits, 16).ok()?;
    Some(Color::Rgb(
        ((value >> 16) & 0xff) as u8,
        ((value >> 8) & 0xff) as u8,
        (value & 0xff) as u8,
    ))
}

fn colour(value: &str, fallback: Color) -> Color {
    parse_hex(value).unwrap_or(fallback)
}

/// Names live for the whole process, so leaking is the cheapest way to keep
/// `Theme` `Copy` while still loading names at runtime. Bounded by the number of
/// themes on disk.
fn intern(value: &str) -> &'static str {
    Box::leak(value.to_owned().into_boxed_str())
}

impl super::Theme {
    fn from_file(file: ThemeFile, source: ThemeSource) -> Self {
        let background = colour(&file.base.background, Color::Black);
        let text = colour(&file.base.foreground, Color::White);
        let accent = colour(&file.base.accent, text);
        let muted = colour(&file.base.muted, text);
        let selection = colour(&file.base.selection, background);

        let region = |table: &RegionTable| Region {
            fg: colour(&table.fg, text),
            bg: colour(&table.bg, background),
            border: colour(&table.border, muted),
            border_active: colour(&table.border_active, accent),
            title: colour(&table.title, accent),
            item_selected_fg: colour(&table.item_selected_fg, text),
            item_selected_bg: colour(&table.item_selected_bg, selection),
        };

        let gradient_start = file
            .base
            .gradient
            .first()
            .and_then(|value| parse_hex(value))
            .unwrap_or(accent);
        let gradient_end = file
            .base
            .gradient
            .get(1)
            .and_then(|value| parse_hex(value))
            .unwrap_or(gradient_start);

        let sidebar = region(&file.sidebar);
        let workspace = region(&file.workspace);

        Self {
            name: intern(&file.name),
            source: match file.source.as_str() {
                "omarchy" => ThemeSource::Omarchy,
                "superfile" => ThemeSource::Superfile,
                _ => source,
            },
            mode: if file.mode == "light" {
                ThemeMode::Light
            } else {
                ThemeMode::Dark
            },
            background,
            panel: colour(&file.modal.bg, background),
            text,
            muted,
            border: workspace.border,
            focus: accent,
            selection,
            cyan: colour(&file.ansi.cyan, colour(&file.ansi.blue, accent)),
            green: colour(&file.ansi.green, accent),
            yellow: colour(&file.ansi.yellow, accent),
            red: colour(&file.ansi.red, accent),
            purple: colour(&file.ansi.magenta, accent),
            orange: colour(&file.ansi.orange, accent),
            sidebar,
            workspace,
            footer: Region {
                fg: colour(&file.footer.fg, text),
                bg: colour(&file.footer.bg, background),
                border: colour(&file.footer.border, muted),
                border_active: colour(&file.footer.border_active, accent),
                title: accent,
                item_selected_fg: text,
                item_selected_bg: selection,
            },
            modal: Modal {
                fg: colour(&file.modal.fg, text),
                bg: colour(&file.modal.bg, background),
                border_active: colour(&file.modal.border_active, accent),
                cancel_fg: colour(&file.modal.cancel_fg, background),
                cancel_bg: colour(&file.modal.cancel_bg, accent),
                confirm_fg: colour(&file.modal.confirm_fg, background),
                confirm_bg: colour(&file.modal.confirm_bg, accent),
            },
            status_colors: StatusColors {
                cursor: colour(&file.status.cursor, accent),
                correct: colour(&file.status.correct, accent),
                error: colour(&file.status.error, accent),
                hint: colour(&file.status.hint, muted),
                cancel: colour(&file.status.cancel, muted),
                hotkey: colour(&file.status.hotkey, accent),
            },
            gradient: [gradient_start, gradient_end],
        }
    }

    /// Parses one theme file. Returns the reason on failure so the caller can
    /// tell the user which of their files is wrong.
    pub fn parse(source_text: &str, origin: ThemeSource) -> Result<Self, String> {
        let file: ThemeFile = toml::from_str(source_text).map_err(|error| error.to_string())?;
        Ok(Self::from_file(file, origin))
    }
}

// ----------------------------------------------------------------- registry

struct Registry {
    themes: Vec<super::Theme>,
    problems: Vec<String>,
}

fn registry() -> &'static RwLock<Registry> {
    static REGISTRY: OnceLock<RwLock<Registry>> = OnceLock::new();
    REGISTRY.get_or_init(|| RwLock::new(load_registry()))
}

fn user_theme_dir() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))?;
    Some(base.join("omarag").join("themes"))
}

fn load_registry() -> Registry {
    let mut themes = Vec::with_capacity(BUNDLED_THEMES.len() + 1);
    let mut problems = Vec::new();

    for (stem, body) in BUNDLED_THEMES {
        match super::Theme::parse(body, ThemeSource::Bundled) {
            Ok(theme) => themes.push(theme),
            // A broken bundled theme is our bug, not the user's; surface it
            // rather than silently shipping a shorter list.
            Err(error) => problems.push(format!("bundled theme {stem}: {error}")),
        }
    }

    // A user theme with the same name replaces the bundled one.
    if let Some(dir) = user_theme_dir()
        && let Ok(entries) = fs::read_dir(&dir)
    {
        let mut paths: Vec<PathBuf> = entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| path.extension().is_some_and(|ext| ext == "toml"))
            .collect();
        paths.sort();
        for path in paths {
            let name = path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned();
            match fs::read_to_string(&path)
                .map_err(|error| error.to_string())
                .and_then(|body| super::Theme::parse(&body, ThemeSource::User))
            {
                Ok(theme) => {
                    if let Some(slot) = themes.iter_mut().find(|item| item.name == theme.name) {
                        *slot = theme;
                    } else {
                        themes.push(theme);
                    }
                }
                // One bad file must never stop the app from starting.
                Err(error) => problems.push(format!("{name}: {error}")),
            }
        }
    }

    themes.sort_by(|left, right| left.name.to_lowercase().cmp(&right.name.to_lowercase()));

    // The live desktop palette is always last so its index is stable as the
    // bundled list grows.
    themes.push(super::omarchy_theme());

    Registry { themes, problems }
}

/// Number of selectable themes.
pub fn theme_count() -> usize {
    registry().read().map(|r| r.themes.len()).unwrap_or(1)
}

/// Theme by index, wrapping. Index 0 always exists.
pub fn theme_at(index: usize) -> super::Theme {
    let registry = registry().read().expect("theme registry");
    let count = registry.themes.len().max(1);
    registry
        .themes
        .get(index % count)
        .copied()
        .unwrap_or_else(super::omarchy_theme)
}

/// Index of a theme by name, for restoring a saved preference.
pub fn index_of(name: &str) -> Option<usize> {
    registry()
        .read()
        .ok()?
        .themes
        .iter()
        .position(|theme| theme.name.eq_ignore_ascii_case(name))
}

/// Every theme, for the picker.
pub fn all_themes() -> Vec<super::Theme> {
    registry()
        .read()
        .map(|registry| registry.themes.clone())
        .unwrap_or_default()
}

/// Files that failed to load, reported once at startup.
pub fn problems() -> Vec<String> {
    registry()
        .read()
        .map(|registry| registry.problems.clone())
        .unwrap_or_default()
}

/// Re-reads the live desktop palette into the last slot.
pub(crate) fn refresh_system_slot(theme: super::Theme) {
    if let Ok(mut registry) = registry().write()
        && let Some(last) = registry.themes.last_mut()
    {
        *last = theme;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_bundled_theme_parses() {
        let mut failures = Vec::new();
        for (stem, body) in BUNDLED_THEMES {
            if let Err(error) = crate::Theme::parse(body, ThemeSource::Bundled) {
                failures.push(format!("{stem}: {error}"));
            }
        }
        assert!(failures.is_empty(), "unparseable themes: {failures:#?}");
        assert!(
            BUNDLED_THEMES.len() >= 30,
            "expected the imported set, found {}",
            BUNDLED_THEMES.len()
        );
    }

    #[test]
    fn selection_is_never_invisible() {
        // A selection fill equal to the background would make the highlighted
        // row vanish; the importer guards against it, this holds the line.
        for (stem, body) in BUNDLED_THEMES {
            let theme = crate::Theme::parse(body, ThemeSource::Bundled).expect(stem);
            assert_ne!(theme.selection, theme.background, "{stem}");
            assert_ne!(
                theme.sidebar.item_selected_bg, theme.sidebar.bg,
                "{stem} sidebar"
            );
        }
    }

    #[test]
    fn regions_can_differ_so_panels_read_apart() {
        // Not every theme distinguishes them, but the model must carry it.
        let distinct = BUNDLED_THEMES.iter().filter(|(_, body)| {
            crate::Theme::parse(body, ThemeSource::Bundled)
                .map(|theme| {
                    theme.sidebar.border_active != theme.workspace.border_active
                        || theme.footer.border_active != theme.workspace.border_active
                })
                .unwrap_or(false)
        });
        assert!(
            distinct.count() > 5,
            "region roles collapsed to a single accent everywhere"
        );
    }

    /// Relative luminance, per WCAG.
    fn luminance(colour: Color) -> f64 {
        let Color::Rgb(r, g, b) = colour else {
            return 0.0;
        };
        let channel = |value: u8| {
            let v = f64::from(value) / 255.0;
            if v <= 0.03928 {
                v / 12.92
            } else {
                ((v + 0.055) / 1.055).powf(2.4)
            }
        };
        0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
    }

    fn contrast(left: Color, right: Color) -> f64 {
        let (a, b) = (luminance(left), luminance(right));
        (a.max(b) + 0.05) / (a.min(b) + 0.05)
    }

    #[test]
    fn every_hairline_is_actually_visible() {
        // The hairline is the region boundary; below roughly 2:1 it stops
        // reading as one and the three-pane structure disappears.
        for (stem, body) in BUNDLED_THEMES {
            let theme = crate::Theme::parse(body, ThemeSource::Bundled).expect(stem);
            for (name, region) in [
                ("sidebar", theme.sidebar),
                ("workspace", theme.workspace),
                ("footer", theme.footer),
            ] {
                let ratio = contrast(region.border, region.bg);
                assert!(
                    ratio >= 2.0,
                    "{stem} {name}: hairline contrast {ratio:.2} is invisible"
                );
            }
        }
    }

    #[test]
    fn hex_parsing_accepts_the_shapes_upstream_uses() {
        assert_eq!(parse_hex("#1e1e2e"), Some(Color::Rgb(0x1e, 0x1e, 0x2e)));
        assert_eq!(parse_hex("0x1E1E2E"), Some(Color::Rgb(0x1e, 0x1e, 0x2e)));
        assert_eq!(parse_hex("\"#1e1e2e\""), Some(Color::Rgb(0x1e, 0x1e, 0x2e)));
        assert_eq!(parse_hex("nope"), None);
        assert_eq!(parse_hex("#12345"), None);
    }
}
