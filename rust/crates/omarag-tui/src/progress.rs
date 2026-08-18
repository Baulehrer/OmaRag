//! Progress bars.
//!
//! The filled run is drawn as a gradient between the theme's two stops, the way
//! superfile draws its process bars. A gradient is not decoration here: it gives
//! a long bar a readable direction, so a glance tells you roughly how far along
//! something is without reading the number.
//!
//! Partial cells use the eighth-block glyphs, so a bar advances smoothly at one
//! eighth of a column rather than jumping a whole cell at a time.

use crate::Theme;
use ratatui::{
    style::{Color, Style},
    text::{Line, Span},
};

/// Eighth-width blocks, from one eighth to full.
const PARTIALS: [&str; 8] = ["▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"];
const TRACK: &str = "░";

/// Blends two colours. Non-RGB colours (a terminal default) cannot be blended,
/// so the left one stands.
fn blend(from: Color, to: Color, percent: u16) -> Color {
    let (Color::Rgb(fr, fg, fb), Color::Rgb(tr, tg, tb)) = (from, to) else {
        return from;
    };
    let mix = |a: u8, b: u8| -> u8 {
        ((u16::from(a) * (100 - percent) + u16::from(b) * percent) / 100) as u8
    };
    Color::Rgb(mix(fr, tr), mix(fg, tg), mix(fb, tb))
}

/// A gradient bar `width` columns wide filled to `ratio` (0.0–1.0).
///
/// `ratio` is clamped, and NaN is treated as empty — a bar must never render
/// from a value the backend could not compute.
pub fn bar(width: u16, ratio: f64, theme: &Theme) -> Vec<Span<'static>> {
    bar_with(
        width,
        ratio,
        theme.gradient[0],
        theme.gradient[1],
        theme.muted,
    )
}

/// A gradient bar in explicit colours, for places that carry their own meaning
/// (a failed job stays red rather than adopting the accent).
pub fn bar_with(
    width: u16,
    ratio: f64,
    from: Color,
    to: Color,
    track: Color,
) -> Vec<Span<'static>> {
    let width = width as usize;
    if width == 0 {
        return Vec::new();
    }
    let ratio = if ratio.is_finite() {
        ratio.clamp(0.0, 1.0)
    } else {
        0.0
    };

    let eighths = (ratio * (width * 8) as f64).round() as usize;
    let full = eighths / 8;
    let remainder = eighths % 8;

    let mut spans = Vec::with_capacity(width.min(full + 2));
    for column in 0..full.min(width) {
        // Interpolate across the whole bar, not just the filled part, so the
        // colour at a given column means the same thing at any fill level.
        let position = if width > 1 {
            (column * 100 / (width - 1)) as u16
        } else {
            0
        };
        spans.push(Span::styled(
            PARTIALS[7],
            Style::default().fg(blend(from, to, position)),
        ));
    }
    if full < width && remainder > 0 {
        let position = if width > 1 {
            (full * 100 / (width - 1)) as u16
        } else {
            0
        };
        spans.push(Span::styled(
            PARTIALS[remainder - 1],
            Style::default().fg(blend(from, to, position)),
        ));
    }
    let drawn = spans.len();
    if drawn < width {
        spans.push(Span::styled(
            TRACK.repeat(width - drawn),
            Style::default().fg(track),
        ));
    }
    spans
}

/// An indeterminate bar: a lit block sweeping a dim track, for work whose extent
/// the backend genuinely cannot report. Never fake a percentage.
pub fn indeterminate(width: u16, tick: u64, theme: &Theme) -> Vec<Span<'static>> {
    let width = width as usize;
    if width == 0 {
        return Vec::new();
    }
    let run = (width / 4).max(2);
    let span = width + run;
    let head = (tick as usize / 2) % span;

    let mut cells = vec![false; width];
    for offset in 0..run {
        if head >= offset && head - offset < width {
            cells[head - offset] = true;
        }
    }
    cells
        .into_iter()
        .enumerate()
        .map(|(column, lit)| {
            let position = if width > 1 {
                (column * 100 / (width - 1)) as u16
            } else {
                0
            };
            if lit {
                Span::styled(
                    PARTIALS[7],
                    Style::default().fg(blend(theme.gradient[0], theme.gradient[1], position)),
                )
            } else {
                Span::styled(TRACK, Style::default().fg(theme.muted))
            }
        })
        .collect()
}

/// A labelled bar: `label ▉▉▊░░  42%`, with the value right of the track.
pub fn labelled(label: &str, width: u16, ratio: f64, detail: &str, theme: &Theme) -> Line<'static> {
    let mut spans = Vec::new();
    if !label.is_empty() {
        spans.push(Span::styled(format!("{label} "), theme.meta()));
    }
    spans.extend(bar(width, ratio, theme));
    let percent = if ratio.is_finite() {
        (ratio.clamp(0.0, 1.0) * 100.0).round() as u16
    } else {
        0
    };
    spans.push(Span::styled(format!(" {percent:>3}%"), theme.body()));
    if !detail.is_empty() {
        spans.push(Span::styled(format!("  {detail}"), theme.meta()));
    }
    Line::from(spans)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(spans: &[Span<'_>]) -> String {
        spans.iter().map(|span| span.content.as_ref()).collect()
    }

    #[test]
    fn a_bar_always_occupies_exactly_its_width() {
        let theme = Theme::default();
        for width in [1u16, 2, 7, 20, 40] {
            for ratio in [0.0, 0.01, 0.5, 0.999, 1.0] {
                let rendered = text(&bar(width, ratio, &theme));
                assert_eq!(
                    rendered.chars().count(),
                    width as usize,
                    "width {width} ratio {ratio} rendered {rendered:?}"
                );
            }
        }
    }

    #[test]
    fn a_full_bar_has_no_track_and_an_empty_one_is_all_track() {
        let theme = Theme::default();
        assert_eq!(text(&bar(10, 1.0, &theme)), "█".repeat(10));
        assert_eq!(text(&bar(10, 0.0, &theme)), TRACK.repeat(10));
    }

    #[test]
    fn a_value_the_backend_could_not_compute_renders_empty() {
        // Never invent progress from a NaN or an out-of-range ratio.
        let theme = Theme::default();
        assert_eq!(text(&bar(8, f64::NAN, &theme)), TRACK.repeat(8));
        assert_eq!(text(&bar(8, -1.0, &theme)), TRACK.repeat(8));
        assert_eq!(text(&bar(8, 4.0, &theme)), "█".repeat(8));
    }

    #[test]
    fn partial_cells_make_the_bar_advance_smoothly() {
        // An eighth of a column must be visible, or a slow job looks stuck.
        let theme = Theme::default();
        let eighth = text(&bar(8, 1.0 / 64.0, &theme));
        assert!(eighth.starts_with('▏'), "got {eighth:?}");
    }

    #[test]
    fn the_gradient_actually_runs_between_the_two_stops() {
        let theme = Theme::default();
        let spans = bar(20, 1.0, &theme);
        assert_ne!(
            spans.first().unwrap().style.fg,
            spans.last().unwrap().style.fg,
            "a gradient that does not change colour is just a bar"
        );
    }

    #[test]
    fn an_indeterminate_bar_keeps_its_width_and_moves() {
        let theme = Theme::default();
        let first = text(&indeterminate(16, 0, &theme));
        let later = text(&indeterminate(16, 8, &theme));
        assert_eq!(first.chars().count(), 16);
        assert_eq!(later.chars().count(), 16);
        assert_ne!(first, later, "the sweep must advance with the tick");
    }
}
