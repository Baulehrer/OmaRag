//! Developer inspection harness: renders TUI scenes to ANSI text on stdout.
//!
//! Usage: cargo run -p omarag-tui --example inspect -- [scene] [width] [height]
//!        cargo run -p omarag-tui --example inspect -- list

use omarag_app::{
    AppState, ConnectionState, FileBrowserEntry, InteractionLevel, ModelCatalogEntry,
    ModelCategory, ModelFit, ModelPackage, ModelPackageItem, ModelSource, Overlay, View,
};
use omarag_domain::{
    AnswerCacheStatus, Citation, DocumentSummary, JobSnapshot, JobStatus, RunReceipt, SourceCheck,
    WorkspaceSummary,
};
use omarag_tui::{LoadedModel, ModelRoleStatus, RuntimeMetrics, Theme, render_with_metrics};
use ratatui::{Terminal, backend::TestBackend, buffer::Buffer, style::Color};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let scene = args.next().unwrap_or_else(|| "chat".into());
    let width: u16 = args.next().and_then(|v| v.parse().ok()).unwrap_or(150);
    let height: u16 = args.next().and_then(|v| v.parse().ok()).unwrap_or(42);

    if scene == "list" {
        for name in SCENES {
            println!("{name}");
        }
        return Ok(());
    }

    let theme = Theme::at(
        std::env::var("OMARAG_THEME")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(0),
    );
    let scenes: Vec<&str> = if scene == "all" {
        SCENES.to_vec()
    } else {
        scene.split(',').collect()
    };

    for name in scenes {
        let state = build_scene(name);
        let mut terminal = Terminal::new(TestBackend::new(width, height))?;
        terminal.draw(|frame| render_with_metrics(frame, &state, &theme, &demo_metrics()))?;
        let buffer = terminal.backend().buffer().clone();
        println!("\n\x1b[0m╔══ {name}  {width}x{height} ══╗");
        print!("{}", ansi(&buffer));
        println!("\x1b[0m╚════════════════════════════╝");
    }
    Ok(())
}

const SCENES: &[&str] = &[
    "chat",
    "chat-empty",
    "chat-streaming",
    "chat-draft",
    "chat-error",
    "chat-long",
    "books",
    "books-empty",
    "indexing",
    "history",
    "retrieval",
    "sources",
    "quality",
    "backups",
    "presets",
    "catalog",
    "runtime",
    "settings",
    "themes",
    "help",
    "palette",
    "browser",
    "libraries",
    "doc-details",
    "quit",
    "profiles",
    "scope",
];

fn build_scene(name: &str) -> AppState {
    let mut state = demo_state();
    match name {
        "chat" => {}
        "chat-empty" => {
            state.documents.clear();
            state.chat.answer.clear();
            state.chat.citations.clear();
            state.chat.receipt = None;
            state.chat.submitted_question.clear();
            state.chat.question.set(String::new());
        }
        "chat-draft" => {
            state.chat.answer =
                "Reinforced concrete durability depends on the exposure class [E1].".into();
            state.chat.draft =
                "The cover must additionally account for the execution tolerance, which".into();
            state.chat.request_pending = true;
            state.chat.phase_label = "Searching & drafting".into();
        }
        "chat-streaming" => {
            state.chat.answer =
                "Reinforced concrete durability depends on the exposure class and the".into();
            state.chat.request_pending = true;
            state.chat.phase = "generating".into();
            state.chat.phase_label = "Searching & drafting".into();
        }
        "chat-error" => {
            state.chat.answer.clear();
            state.chat.error =
                Some("The answer service did not respond within 60 seconds. The local model may still be loading.".into());
        }
        "chat-long" => {
            state.chat.answer = long_answer();
            state.chat.citations = long_citations();
        }
        "books" => state.navigate_view(View::Books),
        "books-empty" => {
            state.navigate_view(View::Books);
            state.documents.clear();
        }
        "indexing" => {
            state.navigate_view(View::Indexing);
            state.jobs = demo_jobs();
        }
        "history" => state.navigate_view(View::History),
        "retrieval" => {
            state.interaction_level = InteractionLevel::Workshop;
            state.navigate_view(View::Retrieval);
        }
        "sources" => {
            state.interaction_level = InteractionLevel::Workshop;
            state.navigate_view(View::Sources);
        }
        "quality" => {
            state.interaction_level = InteractionLevel::Workshop;
            state.navigate_view(View::Quality);
        }
        "backups" => {
            state.interaction_level = InteractionLevel::Workshop;
            state.navigate_view(View::Backups);
        }
        "presets" => {
            state.navigate_view(View::FoundryOverview);
            state.model_manager.packages = demo_packages();
        }
        "catalog" => {
            state.navigate_view(View::Models);
            state.model_manager.entries = demo_entries();
            state.model_manager.scanned = 187;
            state.model_manager.compatible = 24;
        }
        "runtime" => {
            state.interaction_level = InteractionLevel::Workshop;
            state.navigate_view(View::System);
        }
        "settings" => state.navigate_view(View::Settings),
        "themes" => state.navigate_view(View::Themes),
        "help" => state.overlay = Some(Overlay::Help),
        "palette" => state.overlay = Some(Overlay::Palette),
        "browser" => {
            state.overlay = Some(Overlay::FileBrowser);
            state.file_browser.current_dir = "/home/anna/Knowledge".into();
            state.file_browser.entries = vec![
                entry("..", true),
                entry("Concrete", true),
                entry("Standards", true),
                entry("Concrete Design Handbook.pdf", false),
                entry(
                    "Eurocode 2 — Design of Concrete Structures Part 1-1.pdf",
                    false,
                ),
                entry("Materials and Durability.pdf", false),
            ];
            state.file_browser.selected =
                vec!["/home/anna/Knowledge/Concrete Design Handbook.pdf".into()];
            state.file_browser.cursor = 4;
        }
        "libraries" => state.overlay = Some(Overlay::Workspaces),
        "doc-details" => {
            state.navigate_view(View::Books);
            state.overlay = Some(Overlay::DocumentDetails);
        }
        "quit" => state.overlay = Some(Overlay::ConfirmQuit),
        "profiles" => {
            state.navigate_view(View::Books);
            state.overlay = Some(Overlay::WorkspaceProfile);
        }
        "scope" => state.overlay = Some(Overlay::BookScope),
        other => panic!("unknown scene: {other}"),
    }
    state
}

/// Renders the buffer as text. Colour is emitted only when `OMARAG_COLOR=1`,
/// and then only when the style actually changes, so the output stays readable.
fn ansi(buffer: &Buffer) -> String {
    let color = std::env::var("OMARAG_COLOR").is_ok_and(|value| value == "1");
    let mut out = String::new();
    for y in buffer.area.top()..buffer.area.bottom() {
        let mut current = String::new();
        for x in buffer.area.left()..buffer.area.right() {
            let cell = &buffer[(x, y)];
            if color {
                let style = format!("{}{}", sgr(cell.fg, true), sgr(cell.bg, false));
                if style != current {
                    out.push_str("\x1b[0m");
                    out.push_str(&style);
                    current = style;
                }
            }
            out.push_str(cell.symbol());
        }
        if color {
            out.push_str("\x1b[0m");
        }
        out.push('\n');
    }
    out
}

fn sgr(color: Color, foreground: bool) -> String {
    let base = if foreground { 38 } else { 48 };
    match color {
        Color::Rgb(r, g, b) => format!("\x1b[{base};2;{r};{g};{b}m"),
        Color::Reset => String::new(),
        Color::Black => format!("\x1b[{};0;0;0m", base),
        Color::White => format!("\x1b[{base};2;255;255;255m"),
        _ => String::new(),
    }
}

fn demo_state() -> AppState {
    let mut state = AppState {
        connection: ConnectionState::Connected,
        active_workspace: Some("ws-concrete".into()),
        workspaces: vec![
            WorkspaceSummary {
                id: "ws-concrete".into(),
                name: "Concrete & Structures".into(),
                path: "/home/anna/.local/share/omarag/libraries/concrete".into(),
                read_only: false,
                updated_at: "2026-07-26T20:00:00Z".into(),
                etag: "demo".into(),
            },
            WorkspaceSummary {
                id: "ws-timber".into(),
                name: "Timber Engineering".into(),
                path: "/home/anna/.local/share/omarag/libraries/timber".into(),
                read_only: false,
                updated_at: "2026-07-20T10:00:00Z".into(),
                etag: "demo2".into(),
            },
        ],
        ..AppState::default()
    };
    state.documents = vec![
        document("Concrete Design Handbook.pdf", 612),
        document(
            "Eurocode 2 — Design of Concrete Structures Part 1-1.pdf",
            227,
        ),
        document("Materials and Durability.pdf", 344),
    ];
    state
        .chat
        .question
        .set("What controls concrete durability?");
    state.chat.submitted_question = "What controls concrete durability?".into();
    state.chat.answer = "**Reinforced concrete durability** depends on **exposure class**, **cover**, crack control and execution quality [E1]. The indexed handbook recommends checking these as one system rather than isolated values [E2].".into();
    state.chat.citations = vec![
        demo_citation(
            "E1",
            "concrete-design-handbook",
            "Concrete Design Handbook",
            "/home/anna/Knowledge/Concrete Design Handbook.pdf",
            &[184, 185],
            true,
        ),
        demo_citation(
            "E2",
            "eurocode-2",
            "Eurocode 2 — Design of Concrete Structures Part 1-1",
            "/home/anna/Knowledge/Eurocode 2.pdf",
            &[72, 73],
            false,
        ),
    ];
    state.chat.receipt = Some(demo_receipt());
    state
}

fn long_answer() -> String {
    let mut answer = String::new();
    answer.push_str("## Durability of reinforced concrete\n\n");
    answer.push_str("**Exposure class** is the starting point for every durability check [E1]. It encodes the chemical and physical environment the structure will live in, and it drives the minimum cover, the maximum water/cement ratio and the minimum strength class.\n\n");
    answer.push_str("The handbook is explicit that these values form **one system**: relaxing cover while keeping the same crack width limit does not preserve the intended service life [E2].\n\n");
    answer.push_str("### Practical checks\n\n");
    answer.push_str("1. Determine the exposure class per element face, not per structure.\n");
    answer.push_str("2. Read the minimum cover from the table and add the execution tolerance.\n");
    answer.push_str("3. Verify crack width against the same exposure class [E3].\n");
    answer
        .push_str("4. Confirm the concrete mix actually delivered matches the specification.\n\n");
    answer.push_str("A very long sentence follows in order to exercise wrapping behaviour in narrow terminals, because real answers frequently contain long uninterrupted clauses that must wrap gracefully instead of being truncated at the pane boundary or spilling into the inspector column [E4].\n");
    answer
}

fn long_citations() -> Vec<Citation> {
    vec![
        demo_citation(
            "E1",
            "concrete-design-handbook",
            "Concrete Design Handbook",
            "/home/anna/Knowledge/Concrete Design Handbook.pdf",
            &[184, 185, 186],
            true,
        ),
        demo_citation(
            "E2",
            "eurocode-2",
            "Eurocode 2 — Design of Concrete Structures Part 1-1: General rules and rules for buildings",
            "/home/anna/Knowledge/Eurocode 2.pdf",
            &[72, 73],
            false,
        ),
        demo_citation(
            "E3",
            "materials",
            "Materials and Durability",
            "/home/anna/Knowledge/Materials and Durability.pdf",
            &[301],
            true,
        ),
        demo_citation(
            "E4",
            "concrete-design-handbook",
            "Concrete Design Handbook",
            "/home/anna/Knowledge/Concrete Design Handbook.pdf",
            &[42],
            false,
        ),
    ]
}

fn demo_citation(
    evidence_id: &str,
    document_id: &str,
    title: &str,
    source: &str,
    pages: &[u32],
    has_picture: bool,
) -> Citation {
    Citation {
        evidence_id: Some(evidence_id.into()),
        prompt_evidence_id: Some(evidence_id.into()),
        chunk_id: format!("{document_id}-chunk"),
        chunk_ids: vec![format!("{document_id}-chunk")],
        document_id: Some(document_id.into()),
        logical_document_id: Some(document_id.into()),
        source_uri: Some(source.into()),
        document_title: Some(title.into()),
        pages: pages.to_vec(),
        headings: vec!["Durability design".into()],
        element_types: vec![if has_picture { "picture" } else { "text" }.into()],
        doc_item_refs: Vec::new(),
        picture_refs: has_picture.then(|| "#/pictures/0".into()).into_iter().collect(),
        primary_anchors: Vec::new(),
        context_anchors: Vec::new(),
        excerpt: "Durability design combines exposure class, cover and crack control into a single verification chain.".into(),
        excerpt_char_start: Some(0),
        excerpt_char_end: Some(74),
        chunk_content_hash: None,
        retrieval_rank: Some(1),
        rerank_score: Some(0.94),
        claim_ids: vec!["C1".into()],
        retrieval_paths: vec!["hybrid".into()],
        relevance_score: Some(0.94),
        book: None,
        verification_status: "verified".into(),
    }
}

fn demo_receipt() -> RunReceipt {
    RunReceipt {
        session_id: "conversation-demo".into(),
        turn: 2,
        cache_status: AnswerCacheStatus::Hit,
        total_ms: 18.0,
        source_count: 2,
        reused_source_count: 1,
        new_source_count: 1,
        source_check: SourceCheck::Verified,
        phase_timings_ms: Default::default(),
        retrieval_mode: "hybrid".into(),
        rerank_status: "applied".into(),
        complexity: "standard".into(),
        route: "book-kg+hybrid".into(),
        facets: Vec::new(),
        budgets: Default::default(),
        candidate_count: 40,
        selected_count: 2,
        cut_reason: "score_gap".into(),
        facet_coverage: Default::default(),
        fallbacks: Vec::new(),
        model_digests: Default::default(),
        prompt_tokens: Some(320),
        output_tokens: Some(90),
        tokens_per_second: Some(24.0),
        time_to_first_token_ms: Some(480.0),
        singleflight_status: "none".into(),
        abstention: "none".into(),
        rejected_claims: 0,
        done_reason: "stop".into(),
        retrieval_stages: Vec::new(),
        escalation_reasons: Vec::new(),
        calibrator_digest: None,
        calibrator_status: "unknown".into(),
        verifier_digest: None,
        verifier_status: "not-run".into(),
        typed_evidence_status: "unknown".into(),
    }
}

fn document(title: &str, pages: u32) -> DocumentSummary {
    DocumentSummary {
        id: title.to_lowercase().replace(' ', "-"),
        title: title.into(),
        source: format!("/home/anna/Knowledge/{title}"),
        segment_document_ids: vec![],
        page_count: Some(pages),
        size_bytes: 24_000_000,
        archive_mode: "reflink".into(),
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
        structure_mode: "body-headings".into(),
        structure_confidence: 0.91,
        toc_found: true,
        index_found: true,
        glossary_found: false,
        fallback_used: false,
    }
}

fn job(id: &str, status: JobStatus, progress: f64, phase: &str) -> JobSnapshot {
    JobSnapshot {
        id: id.into(),
        workspace_id: "ws-concrete".into(),
        kind: "ingest".into(),
        status,
        progress,
        phase: phase.into(),
        payload: serde_json::Value::Null,
        result: None,
        error: None,
        created_at: "2026-07-26T19:00:00Z".into(),
        updated_at: "2026-07-26T20:00:00Z".into(),
        last_event_id: None,
        checkpoint: None,
        progress_detail: None,
        pinned: false,
    }
}

fn demo_jobs() -> std::collections::BTreeMap<omarag_domain::JobId, JobSnapshot> {
    let mut jobs = std::collections::BTreeMap::new();
    jobs.insert(
        "job-1".to_string(),
        job("job-1", JobStatus::Running, 0.42, "Embedding chunks"),
    );
    jobs.insert(
        "job-2".to_string(),
        job("job-2", JobStatus::Completed, 1.0, "Verifying index"),
    );
    jobs.insert(
        "job-3".to_string(),
        job("job-3", JobStatus::Failed, 0.31, "Converting pages"),
    );
    jobs
}

fn demo_packages() -> Vec<ModelPackage> {
    vec![
        package(1, "Fast", "One compact model family for chat and vision."),
        package(
            2,
            "Balanced",
            "Separated chat and retrieval for higher quality.",
        ),
        package(3, "Quality", "BGE embedding and reranking tuned together."),
    ]
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

fn demo_entries() -> Vec<ModelCatalogEntry> {
    vec![
        model(
            "qwen3-embedding:0.6b",
            "Efficient multilingual embedding",
            1,
        ),
        model("bge-m3:567m", "Dense, sparse and multi-vector retrieval", 2),
        model("nomic-embed-text:137m", "Small English text embedder", 3),
        model(
            "snowflake-arctic-embed2:568m",
            "Long-context retrieval for very long documents",
            0,
        ),
    ]
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

fn entry(name: &str, is_dir: bool) -> FileBrowserEntry {
    FileBrowserEntry {
        path: format!("/home/anna/Knowledge/{name}"),
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
        animation_tick: 4,
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
