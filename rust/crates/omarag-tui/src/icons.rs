//! Icons.
//!
//! Terminal icons are a trade: they speed up recognition when they carry
//! meaning, and they turn a list into noise when every row has one. So there are
//! three modes — everything, only where it helps, nothing — and every glyph has
//! a plain-text fallback for terminals without a Nerd Font.
//!
//! Nothing in the interface may *depend* on an icon: an icon always sits beside
//! a word, never instead of it.

use omarag_app::{IconMode, IconSet};

/// What an icon stands for. Named by meaning, not by picture, so a set can
/// choose different glyphs without the call sites changing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Icon {
    // Navigation
    Chat,
    Library,
    Models,
    Settings,
    // Objects
    Book,
    Folder,
    FolderOpen,
    Page,
    Figure,
    // State
    Running,
    Done,
    Failed,
    Paused,
    Queued,
    // Model roles
    ModelLoaded,
    ModelIdle,
    // Marks
    Selected,
    Unselected,
    Search,
    Key,
}

impl Icon {
    /// Whether this icon earns its place in the restrained mode.
    ///
    /// The test: does the glyph tell you something the adjacent text does not?
    /// A file-type mark in a long import list does. An icon next to the word
    /// "Settings" does not.
    const fn is_recommended(self) -> bool {
        matches!(
            self,
            Self::Book
                | Self::Folder
                | Self::FolderOpen
                | Self::Page
                | Self::Figure
                | Self::Running
                | Self::Done
                | Self::Failed
                | Self::Paused
                | Self::Queued
                | Self::ModelLoaded
                | Self::ModelIdle
                | Self::Selected
                | Self::Unselected
        )
    }

    /// Nerd Font glyph.
    const fn nerd(self) -> &'static str {
        match self {
            Self::Chat => "\u{f075}",        // nf-fa-comment
            Self::Library => "\u{f02d}",     // nf-fa-book
            Self::Models => "\u{f0e4}",      // nf-fa-dashboard
            Self::Settings => "\u{f013}",    // nf-fa-cog
            Self::Book => "\u{f1c1}",        // nf-fa-file_pdf_o
            Self::Folder => "\u{f07b}",      // nf-fa-folder
            Self::FolderOpen => "\u{f07c}",  // nf-fa-folder_open
            Self::Page => "\u{f15c}",        // nf-fa-file_text
            Self::Figure => "\u{f03e}",      // nf-fa-picture
            Self::Running => "\u{f04b}",     // nf-fa-play
            Self::Done => "\u{f00c}",        // nf-fa-check
            Self::Failed => "\u{f00d}",      // nf-fa-times
            Self::Paused => "\u{f04c}",      // nf-fa-pause
            Self::Queued => "\u{f017}",      // nf-fa-clock
            Self::ModelLoaded => "\u{f111}", // nf-fa-circle
            Self::ModelIdle => "\u{f10c}",   // nf-fa-circle_o
            Self::Selected => "\u{f046}",    // nf-fa-check_square
            Self::Unselected => "\u{f096}",  // nf-fa-square_o
            Self::Search => "\u{f002}",      // nf-fa-search
            Self::Key => "\u{f084}",         // nf-fa-key
        }
    }

    /// Unicode that works without a patched font.
    const fn unicode(self) -> &'static str {
        match self {
            Self::Chat => "◇",
            Self::Library => "▤",
            Self::Models => "◈",
            Self::Settings => "⚙",
            Self::Book => "▪",
            Self::Folder => "▸",
            Self::FolderOpen => "▾",
            Self::Page => "▫",
            Self::Figure => "▨",
            Self::Running => "▶",
            Self::Done => "✓",
            Self::Failed => "✗",
            Self::Paused => "‖",
            Self::Queued => "·",
            Self::ModelLoaded => "●",
            Self::ModelIdle => "○",
            Self::Selected => "×",
            Self::Unselected => " ",
            Self::Search => "/",
            Self::Key => "▪",
        }
    }

    /// Plain ASCII, for terminals that mangle anything else.
    const fn ascii(self) -> &'static str {
        match self {
            Self::Chat => ">",
            Self::Library => "#",
            Self::Models => "*",
            Self::Settings => "%",
            Self::Book => "-",
            Self::Folder => ">",
            Self::FolderOpen => "v",
            Self::Page => "-",
            Self::Figure => "+",
            Self::Running => ">",
            Self::Done => "+",
            Self::Failed => "x",
            Self::Paused => "=",
            Self::Queued => ".",
            Self::ModelLoaded => "*",
            Self::ModelIdle => "o",
            Self::Selected => "x",
            Self::Unselected => " ",
            Self::Search => "/",
            Self::Key => "-",
        }
    }
}

/// Resolves an icon for the user's settings.
///
/// Returns the glyph **with a trailing space**, or an empty string when the
/// icon is suppressed — so a call site can always interpolate it without
/// leaving a stray gap.
pub fn icon(what: Icon, mode: IconMode, set: IconSet) -> String {
    let wanted = match mode {
        IconMode::None => return String::new(),
        IconMode::Recommended => what.is_recommended(),
        IconMode::Full => true,
    };
    if !wanted {
        return String::new();
    }
    let glyph = match set {
        IconSet::NerdFont => what.nerd(),
        IconSet::Unicode => what.unicode(),
        IconSet::Ascii => what.ascii(),
    };
    if glyph.trim().is_empty() {
        return " ".repeat(glyph.chars().count() + 1);
    }
    format!("{glyph} ")
}

/// A status glyph for a job, always resolved — job rows need a mark even when
/// icons are off, so this ignores `IconMode::None` and falls back to Unicode.
pub fn job_glyph(what: Icon, set: IconSet) -> &'static str {
    match set {
        IconSet::NerdFont => what.nerd(),
        IconSet::Unicode => what.unicode(),
        IconSet::Ascii => what.ascii(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn none_suppresses_every_icon() {
        for what in [Icon::Book, Icon::Settings, Icon::Running] {
            assert_eq!(icon(what, IconMode::None, IconSet::NerdFont), "");
        }
    }

    #[test]
    fn recommended_keeps_the_meaningful_ones_and_drops_decoration() {
        // A PDF mark in a file list earns its column; a cog next to the word
        // "Settings" does not.
        assert!(!icon(Icon::Book, IconMode::Recommended, IconSet::Unicode).is_empty());
        assert!(!icon(Icon::Running, IconMode::Recommended, IconSet::Unicode).is_empty());
        assert!(icon(Icon::Settings, IconMode::Recommended, IconSet::Unicode).is_empty());
        assert!(icon(Icon::Chat, IconMode::Recommended, IconSet::Unicode).is_empty());
    }

    #[test]
    fn full_shows_everything() {
        assert!(!icon(Icon::Settings, IconMode::Full, IconSet::Unicode).is_empty());
        assert!(!icon(Icon::Chat, IconMode::Full, IconSet::Unicode).is_empty());
    }

    #[test]
    fn every_icon_resolves_in_every_set() {
        const ALL: [Icon; 20] = [
            Icon::Chat,
            Icon::Library,
            Icon::Models,
            Icon::Settings,
            Icon::Book,
            Icon::Folder,
            Icon::FolderOpen,
            Icon::Page,
            Icon::Figure,
            Icon::Running,
            Icon::Done,
            Icon::Failed,
            Icon::Paused,
            Icon::Queued,
            Icon::ModelLoaded,
            Icon::ModelIdle,
            Icon::Selected,
            Icon::Unselected,
            Icon::Search,
            Icon::Key,
        ];
        for what in ALL {
            for set in [IconSet::NerdFont, IconSet::Unicode, IconSet::Ascii] {
                let rendered = icon(what, IconMode::Full, set);
                assert!(!rendered.is_empty(), "{what:?} in {set:?}");
                // One glyph plus one space: a wider icon would break every
                // hand-aligned column that follows it.
                assert_eq!(
                    rendered.chars().count(),
                    2,
                    "{what:?} in {set:?} rendered {rendered:?}"
                );
            }
        }
    }

    #[test]
    fn ascii_stays_ascii() {
        for what in [Icon::Book, Icon::Done, Icon::Failed, Icon::Folder] {
            let rendered = icon(what, IconMode::Full, IconSet::Ascii);
            assert!(rendered.is_ascii(), "{what:?} rendered {rendered:?}");
        }
    }
}
