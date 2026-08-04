use omarag_app::{
    AppState, ConnectionState, FileBrowserEntry, ModelCatalogEntry, ModelCategory, ModelFit,
    ModelPackage, ModelPackageItem, ModelSource, Overlay, View,
};
use omarag_domain::{
    AnswerCacheStatus, Citation, DocumentSummary, RunReceipt, SourceCheck, WorkspaceSummary,
};
use omarag_tui::{LoadedModel, ModelRoleStatus, RuntimeMetrics, Theme, render_with_metrics};
use ratatui::{Terminal, backend::TestBackend, style::Color};
use std::{fmt::Write as _, fs, path::Path};

const WIDTH: u16 = 156;
const HEIGHT: u16 = 42;
const CELL_WIDTH: u16 = 10;
const CELL_HEIGHT: u16 = 20;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let output = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "docs/screenshots".into());
    fs::create_dir_all(&output)?;

    let dashboard = demo_state();
    snapshot(&dashboard, &demo_metrics(), "dashboard", &output)?;

    let mut models = dashboard.clone();
    models.navigate_view(View::FoundryOverview);
    models.model_manager.source = ModelSource::Ollama;
    models.model_manager.category = ModelCategory::Embedding;
    models.model_manager.scanned = 187;
    models.model_manager.compatible = 24;
    models.model_manager.entries = vec![
        model(
            "qwen3-embedding:0.6b",
            "Efficient multilingual embedding",
            1,
        ),
        model("bge-m3:567m", "Dense, sparse and multi-vector retrieval", 2),
        model("nomic-embed-text:137m", "Small English text embedder", 3),
        model("snowflake-arctic-embed2:568m", "Long-context retrieval", 0),
    ];
    models.model_manager.packages = vec![
        package(1, "Fast", "One compact model family for chat and vision."),
        package(
            2,
            "Balanced",
            "Separated chat and retrieval for higher quality.",
        ),
        package(3, "Quality", "BGE embedding and reranking tuned together."),
    ];
    snapshot(&models, &demo_metrics(), "model-foundry", &output)?;

    let mut browser = dashboard.clone();
    browser.overlay = Some(Overlay::FileBrowser);
    browser.file_browser.current_dir = "/home/daedalus/Knowledge".into();
    browser.file_browser.entries = vec![
        entry("..", true),
        entry("Concrete", true),
        entry("Standards", true),
        entry("Concrete Design Handbook.pdf", false),
        entry("Eurocode 2.pdf", false),
        entry("Materials and Durability.pdf", false),
    ];
    browser.file_browser.selected = vec![
        "/home/daedalus/Knowledge/Concrete Design Handbook.pdf".into(),
        "/home/daedalus/Knowledge/Eurocode 2.pdf".into(),
    ];
    browser.file_browser.cursor = 4;
    snapshot(&browser, &demo_metrics(), "knowledge-browser", &output)?;

    let mut help = dashboard;
    help.overlay = Some(Overlay::Help);
    snapshot(&help, &demo_metrics(), "keyboard-and-mouse", &output)?;

    let mut themes = demo_state();
    themes.navigate_view(View::Themes);
    themes.theme_index = 8;
    themes.theme_cursor = 8;
    snapshot(&themes, &demo_metrics(), "themes", &output)?;
    Ok(())
}

fn demo_state() -> AppState {
    let mut state = AppState {
        connection: ConnectionState::Connected,
        active_workspace: Some("ws-concrete".into()),
        workspaces: vec![WorkspaceSummary {
            id: "ws-concrete".into(),
            name: "Concrete Atlas".into(),
            path: "/home/daedalus/.local/share/oracle/libraries/concrete".into(),
            read_only: false,
            updated_at: "2026-07-26T20:00:00Z".into(),
            etag: "demo".into(),
        }],
        ..AppState::default()
    };
    state.documents = vec![
        document("Concrete Design Handbook.pdf", 612),
        document("Eurocode 2.pdf", 227),
        document("Materials and Durability.pdf", 344),
    ];
    state
        .chat
        .question
        .set("What controls concrete durability?");
    state.chat.answer = "Reinforced concrete durability depends on exposure class, cover, crack control and execution quality. The indexed handbook recommends checking these as one system rather than isolated values.".into();
    state.chat.citations = vec![Citation {
        evidence_id: Some("E1".into()),
        chunk_id: "durability-42".into(),
        chunk_ids: vec!["durability-42".into()],
        document_id: Some("concrete-design-handbook".into()),
        logical_document_id: Some("concrete-design-handbook".into()),
        source_uri: Some("/home/daedalus/Knowledge/Concrete Design Handbook.pdf".into()),
        document_title: Some("Concrete Design Handbook".into()),
        pages: vec![184],
        headings: vec!["Durability design".into()],
        element_types: vec!["text".into()],
        doc_item_refs: Vec::new(),
        picture_refs: Vec::new(),
        primary_anchors: Vec::new(),
        context_anchors: Vec::new(),
        excerpt: "Durability design combines exposure class, cover and crack control.".into(),
        retrieval_rank: Some(1),
        rerank_score: Some(0.94),
        book: None,
        verification_status: "verified".into(),
    }];
    state.chat.receipt = Some(RunReceipt {
        session_id: "conversation-demo".into(),
        turn: 2,
        cache_status: AnswerCacheStatus::Hit,
        total_ms: 18.0,
        source_count: 1,
        reused_source_count: 1,
        new_source_count: 0,
        source_check: SourceCheck::Verified,
    });
    state
}

fn document(title: &str, pages: u32) -> DocumentSummary {
    DocumentSummary {
        id: title.to_lowercase().replace(' ', "-"),
        title: title.into(),
        source: format!("/home/daedalus/Knowledge/{title}"),
        segment_document_ids: vec![],
        page_count: Some(pages),
        parser_id: "docling".into(),
        status: "ready".into(),
        imported_at: "2026-07-26T20:00:00Z".into(),
        fingerprint: None,
        generation_id: None,
        cache_status: Some("hit".into()),
        pipeline_stats: Default::default(),
        managed_source: None,
        book: None,
        quality: None,
        pipeline_version: "textbook-v1".into(),
    }
}

fn model(id: &str, description: &str, rank: u8) -> ModelCatalogEntry {
    ModelCatalogEntry {
        id: id.into(),
        source: ModelSource::Ollama,
        category: ModelCategory::Embedding,
        description: description.into(),
        downloads: Some(276_400),
        parameter_count: Some(600_000_000),
        estimated_memory: 720 * 1024 * 1024,
        fit: ModelFit::Comfortable,
        recommended_rank: (rank > 0).then_some(rank),
        ..ModelCatalogEntry::default()
    }
}

fn package(rank: u8, name: &str, summary: &str) -> ModelPackage {
    ModelPackage {
        id: format!("package-{rank}"),
        name: name.into(),
        summary: summary.into(),
        synergy: "Matched embedding and reranking family".into(),
        recommended_rank: rank,
        total_estimated_memory: 2_100_000_000,
        fit: ModelFit::Comfortable,
        models: vec![ModelPackageItem {
            role: ModelCategory::Embedding,
            model: "qwen3-embedding:0.6b".into(),
            download_name: "qwen3-embedding:0.6b".into(),
            source: ModelSource::Ollama,
            installed: rank == 3,
        }],
    }
}

fn entry(name: &str, is_dir: bool) -> FileBrowserEntry {
    FileBrowserEntry {
        path: format!("/home/daedalus/Knowledge/{name}"),
        name: name.into(),
        is_dir,
    }
}

fn demo_metrics() -> RuntimeMetrics {
    RuntimeMetrics {
        cpu_usage: 8.0,
        cpu_count: 12,
        memory_used: 6_300_000_000,
        memory_total: 14_400_000_000,
        memory_available: 8_100_000_000,
        gpu_name: Some("AMD Radeon 760M".into()),
        vram_used: 480_000_000,
        vram_total: 2_000_000_000,
        shared_gpu_memory: 6_700_000_000,
        loaded_models: vec![LoadedModel {
            name: "qwen3.5:2b-q4_K_M".into(),
            size: 1_600_000_000,
            size_vram: 0,
            context_length: 8192,
            parameter_size: "2B".into(),
            quantization: "Q4_K_M".into(),
        }],
        model_roles: vec![
            role("chat", "qwen3.5:2b-q4_K_M", "loaded"),
            role("vl", "qwen3.5:2b-q4_K_M", "loaded"),
            role("embedding", "qwen3-embedding:0.6b", "idle"),
            role(
                "rerank",
                "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
                "idle",
            ),
        ],
        ..RuntimeMetrics::default()
    }
}

fn role(role: &str, model: &str, residency: &str) -> ModelRoleStatus {
    ModelRoleStatus {
        role: role.into(),
        model: Some(model.into()),
        residency: residency.into(),
        shared_with: Vec::new(),
    }
}

fn snapshot(
    state: &AppState,
    metrics: &RuntimeMetrics,
    name: &str,
    output: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let backend = TestBackend::new(WIDTH, HEIGHT);
    let mut terminal = Terminal::new(backend)?;
    terminal
        .draw(|frame| render_with_metrics(frame, state, &Theme::at(state.theme_index), metrics))?;
    let buffer = terminal.backend().buffer();
    let mut svg = String::new();
    writeln!(
        svg,
        r#"<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" viewBox="0 0 {} {}">"#,
        WIDTH * CELL_WIDTH,
        HEIGHT * CELL_HEIGHT,
        WIDTH * CELL_WIDTH,
        HEIGHT * CELL_HEIGHT
    )?;
    writeln!(
        svg,
        r##"<rect width="100%" height="100%" fill="#0d1117"/><g font-family="DejaVu Sans Mono, monospace" font-size="16">"##
    )?;
    for y in 0..HEIGHT {
        for x in 0..WIDTH {
            let cell = &buffer[(x, y)];
            let bg = color(cell.bg, "#0d1117");
            if bg != "#0d1117" {
                writeln!(
                    svg,
                    r#"<rect x="{}" y="{}" width="{}" height="{}" fill="{}"/>"#,
                    x * CELL_WIDTH,
                    y * CELL_HEIGHT,
                    CELL_WIDTH,
                    CELL_HEIGHT,
                    bg
                )?;
            }
            let symbol = cell.symbol();
            if !symbol.trim().is_empty() {
                let fg = color(cell.fg, "#e6e9ee");
                writeln!(
                    svg,
                    r#"<text x="{}" y="{}" fill="{}">{}</text>"#,
                    x * CELL_WIDTH,
                    y * CELL_HEIGHT + 16,
                    fg,
                    escape(symbol)
                )?;
            }
        }
    }
    svg.push_str("</g></svg>\n");
    fs::write(Path::new(output).join(format!("{name}.svg")), svg)?;
    Ok(())
}

fn color(color: Color, fallback: &'static str) -> String {
    match color {
        Color::Rgb(r, g, b) => format!("#{r:02x}{g:02x}{b:02x}"),
        Color::Black => "#000000".into(),
        Color::White => "#ffffff".into(),
        Color::Gray => "#808080".into(),
        Color::DarkGray => "#585b70".into(),
        _ => fallback.into(),
    }
}

fn escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}
