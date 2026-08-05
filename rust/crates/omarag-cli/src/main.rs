use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use omarag_client::{HttpOmaRagClient, OmaRagClient};
use omarag_domain::{
    CreateWorkspace, EvidenceMode, IngestRequest, JobStatus, RunRequest, SearchRequest,
};
use serde::Serialize;
use std::{path::Path, time::Duration};
use url::Url;
use uuid::Uuid;

#[derive(Debug, Parser)]
#[command(version, about = "Scriptable OmaRag client")]
struct Args {
    #[arg(long, env = "OMARAG_URL", default_value = "http://127.0.0.1:8765")]
    url: Url,
    #[arg(long, env = "OMARAG_TOKEN")]
    token: Option<String>,
    #[arg(long, global = true)]
    json: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    Status,
    Doctor,
    Workspace {
        #[command(subcommand)]
        command: WorkspaceCommand,
    },
    Ingest {
        workspace: String,
        source: String,
        #[arg(long)]
        idempotency_key: Option<String>,
    },
    Jobs {
        #[arg(long)]
        workspace: Option<String>,
    },
    Job {
        #[command(subcommand)]
        command: JobCommand,
    },
    Search {
        workspace: String,
        query: String,
        #[arg(long, default_value_t = 10)]
        limit: u32,
        #[arg(long)]
        explain: bool,
    },
    Ask {
        workspace: String,
        question: String,
        #[arg(long, value_enum, default_value_t = EvidenceArg::Strict)]
        evidence: EvidenceArg,
        #[arg(long)]
        wait: bool,
    },
}

#[derive(Debug, Serialize)]
struct DoctorCheck {
    name: &'static str,
    status: &'static str,
    detail: String,
    action: Option<&'static str>,
}

#[derive(Debug, Serialize)]
struct DoctorReport {
    ready: bool,
    omarag_version: String,
    checks: Vec<DoctorCheck>,
}

#[derive(Debug, Subcommand)]
enum WorkspaceCommand {
    List,
    Create {
        name: String,
        #[arg(long)]
        id: Option<String>,
        #[arg(long)]
        read_only: bool,
    },
    Open {
        id: String,
    },
}

#[derive(Debug, Subcommand)]
enum JobCommand {
    Show { id: String },
    Pause { id: String },
    Resume { id: String },
    Cancel { id: String },
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum EvidenceArg {
    Strict,
    Normal,
    Explore,
}

impl From<EvidenceArg> for EvidenceMode {
    fn from(value: EvidenceArg) -> Self {
        match value {
            EvidenceArg::Strict => Self::Strict,
            EvidenceArg::Normal => Self::Normal,
            EvidenceArg::Explore => Self::Explore,
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let client = HttpOmaRagClient::new(args.url, args.token)?;
    match args.command {
        Command::Status => {
            let meta = client.meta().await?;
            if args.json {
                print_json(&meta)?;
            } else {
                println!(
                    "OmaRag {} · Oracle of Metis & Aletheia · API {}",
                    meta.omarag_version, meta.api_version
                );
                println!("Backend: {}", meta.backend_id);
                println!(
                    "Haiku RAG: {}",
                    meta.haiku_version.as_deref().unwrap_or("not installed")
                );
                println!(
                    "RAG ready: {}",
                    if meta.adapter.is_some() { "yes" } else { "no" }
                );
            }
        }
        Command::Doctor => {
            let report = doctor(&client).await;
            if args.json {
                print_json(&report)?;
            } else {
                println!(
                    "OmaRag doctor {} · {}",
                    report.omarag_version,
                    if report.ready {
                        "READY"
                    } else {
                        "NEEDS ATTENTION"
                    }
                );
                for check in &report.checks {
                    println!("{:<5} {:<14} {}", check.status, check.name, check.detail);
                    if let Some(action) = check.action {
                        println!("      Action: {action}");
                    }
                }
            }
            if !report.ready {
                bail!("OmaRag is not ready; follow the actions above");
            }
        }
        Command::Workspace { command } => match command {
            WorkspaceCommand::List => {
                let workspaces = client.list_workspaces().await?;
                if args.json {
                    print_json(&workspaces)?;
                } else if workspaces.is_empty() {
                    println!("No workspaces.");
                } else {
                    for workspace in workspaces {
                        println!("{}\t{}\t{}", workspace.id, workspace.name, workspace.path);
                    }
                }
            }
            WorkspaceCommand::Create {
                name,
                id,
                read_only,
            } => {
                let workspace = client
                    .create_workspace(CreateWorkspace {
                        name,
                        id,
                        read_only,
                    })
                    .await?;
                output(&workspace, args.json, || {
                    format!("Workspace {} ({}) created", workspace.name, workspace.id)
                })?;
            }
            WorkspaceCommand::Open { id } => {
                let workspace = client.open_workspace(id).await?;
                output(&workspace, args.json, || {
                    format!("Workspace {} opened", workspace.name)
                })?;
            }
        },
        Command::Ingest {
            workspace,
            source,
            idempotency_key,
        } => {
            let key = idempotency_key.unwrap_or_else(|| format!("cli-{}", Uuid::new_v4()));
            let result = client
                .ingest(workspace, IngestRequest::file(source), key)
                .await?;
            output(&result, args.json, || {
                format!(
                    "Import job {}{}",
                    result.id,
                    if result.reused { " (reused)" } else { "" }
                )
            })?;
        }
        Command::Jobs { workspace } => {
            let jobs = client.list_jobs(workspace).await?;
            if args.json {
                print_json(&jobs)?;
            } else if jobs.is_empty() {
                println!("No jobs.");
            } else {
                for job in jobs {
                    println!(
                        "{}\t{:?}\t{:.0}%\t{}",
                        job.id,
                        job.status,
                        job.progress * 100.0,
                        job.phase
                    );
                }
            }
        }
        Command::Job { command } => {
            let job = match command {
                JobCommand::Show { id } => client.job_snapshot(id).await?,
                JobCommand::Pause { id } => client.pause_job(id).await?,
                JobCommand::Resume { id } => client.resume_job(id).await?,
                JobCommand::Cancel { id } => client.cancel_job(id).await?,
            };
            output(&job, args.json, || format!("{}: {:?}", job.id, job.status))?;
        }
        Command::Search {
            workspace,
            query,
            limit,
            explain,
        } => {
            let mut request = SearchRequest::new(query);
            request.limit = limit;
            if explain {
                let explanation = client.explain_search(workspace, request).await?;
                if args.json {
                    print_json(&explanation)?;
                } else {
                    println!(
                        "Retrieval: {:.1} ms · {} ranked hits",
                        explanation.timing.total_ms,
                        explanation.ranked.len()
                    );
                    for hit in explanation.ranked {
                        println!(
                            "{:.3}\t{}\t{}",
                            hit.score.unwrap_or_default(),
                            hit.pages
                                .iter()
                                .map(u32::to_string)
                                .collect::<Vec<_>>()
                                .join(","),
                            hit.content.replace('\n', " ")
                        );
                    }
                }
                return Ok(());
            }
            let hits = client.search(workspace, request).await?;
            if args.json {
                print_json(&hits)?;
            } else {
                for hit in hits {
                    println!(
                        "{:.3}\t{}\t{}",
                        hit.score.unwrap_or_default(),
                        hit.pages
                            .iter()
                            .map(u32::to_string)
                            .collect::<Vec<_>>()
                            .join(","),
                        hit.content.replace('\n', " ")
                    );
                }
            }
        }
        Command::Ask {
            workspace,
            question,
            evidence,
            wait,
        } => {
            let run = client
                .start_run(workspace, RunRequest::question(question, evidence.into()))
                .await?;
            if !wait {
                output(&run, args.json, || format!("Run {} started", run.id))?;
                return Ok(());
            }
            let run_id = run.id;
            loop {
                let current = client.get_run(run_id.clone()).await?;
                if matches!(
                    current.status,
                    JobStatus::Completed | JobStatus::Cancelled | JobStatus::Failed
                ) {
                    if args.json {
                        print_json(&current)?;
                    } else if current.status == JobStatus::Completed {
                        println!("{}", current.answer);
                        for citation in current.citations {
                            println!(
                                "[{}] p. {} — {}",
                                citation.chunk_id,
                                citation
                                    .pages
                                    .iter()
                                    .map(u32::to_string)
                                    .collect::<Vec<_>>()
                                    .join(", "),
                                citation.excerpt.replace('\n', " ")
                            );
                        }
                    } else {
                        bail!("Run ended with {:?}: {:?}", current.status, current.error);
                    }
                    break;
                }
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
        }
    }
    Ok(())
}

async fn doctor(client: &HttpOmaRagClient) -> DoctorReport {
    let mut checks = Vec::new();
    let meta = client.meta().await;
    let version = meta.as_ref().map_or_else(
        |_| env!("CARGO_PKG_VERSION").into(),
        |meta| meta.omarag_version.clone(),
    );
    checks.push(match &meta {
        Ok(meta) => DoctorCheck {
            name: "API",
            status: "PASS",
            detail: format!("API {} answered", meta.api_version),
            action: None,
        },
        Err(_) => DoctorCheck {
            name: "API",
            status: "FAIL",
            detail: "The local API did not answer".into(),
            action: Some("Start the OmaRag service, then run doctor again."),
        },
    });
    if let Ok(meta) = &meta {
        checks.push(DoctorCheck {
            name: "Versions",
            status: if meta.omarag_version == env!("CARGO_PKG_VERSION") {
                "PASS"
            } else {
                "WARN"
            },
            detail: format!(
                "CLI {} · API {}",
                env!("CARGO_PKG_VERSION"),
                meta.omarag_version
            ),
            action: (meta.omarag_version != env!("CARGO_PKG_VERSION"))
                .then_some("Restart the daemon from the same OmaRag installation."),
        });
        checks.push(DoctorCheck {
            name: "Haiku RAG",
            status: if meta.adapter.is_some() {
                "PASS"
            } else {
                "FAIL"
            },
            detail: meta
                .haiku_version
                .as_deref()
                .map_or("Adapter is unavailable".into(), |value| {
                    format!("version {value}")
                }),
            action: meta
                .adapter
                .is_none()
                .then_some("Install the supported Haiku RAG runtime in the service environment."),
        });
    }
    match client.health().await {
        Ok(health) => checks.push(DoctorCheck {
            name: "Readiness",
            status: if health.ready { "PASS" } else { "FAIL" },
            detail: health.status,
            action: (!health.ready).then_some("Inspect the failed readiness checks above."),
        }),
        Err(_) => checks.push(DoctorCheck {
            name: "Readiness",
            status: "FAIL",
            detail: "Health endpoint is unavailable".into(),
            action: Some("Restart the local API service."),
        }),
    }
    let workspaces = client.list_workspaces().await.unwrap_or_default();
    let workspace_id = workspaces.first().map(|workspace| workspace.id.clone());
    match client.model_runtime(workspace_id).await {
        Ok(runtime) => {
            let configured = runtime
                .get("roles")
                .and_then(serde_json::Value::as_array)
                .map_or(0, |roles| {
                    roles.iter().filter(|role| !role["model"].is_null()).count()
                });
            checks.push(DoctorCheck {
                name: "Ollama",
                status: "PASS",
                detail: format!("reachable · {configured}/4 roles configured"),
                action: (configured < 4).then_some("Open Models and complete a preset."),
            });
        }
        Err(_) => checks.push(DoctorCheck {
            name: "Ollama",
            status: "FAIL",
            detail: "Model runtime is unreachable".into(),
            action: Some("Start Ollama and verify OLLAMA_HOST or OMARAG_OLLAMA_URL."),
        }),
    }
    let (total, available) = memory_info();
    checks.push(DoctorCheck {
        name: "Memory",
        status: if available >= 1536 * 1024 * 1024 {
            "PASS"
        } else {
            "WARN"
        },
        detail: format!(
            "{} GiB total · {:.1} GiB available",
            total / 1024_u64.pow(3),
            available as f64 / 1024_f64.powi(3)
        ),
        action: (available < 1536 * 1024 * 1024)
            .then_some("Close memory-heavy applications before indexing or asking."),
    });
    let writable = workspaces.iter().all(|workspace| {
        workspace.read_only
            || Path::new(&workspace.path)
                .metadata()
                .is_ok_and(|meta| !meta.permissions().readonly())
    });
    checks.push(DoctorCheck {
        name: "Permissions",
        status: if writable { "PASS" } else { "FAIL" },
        detail: format!("{} registered workspace(s) checked", workspaces.len()),
        action: (!writable)
            .then_some("Grant the service user write access to the affected workspace."),
    });
    let cgroup = Path::new("/sys/fs/cgroup/cgroup.controllers").exists();
    checks.push(DoctorCheck {
        name: "Cgroups",
        status: if cgroup { "PASS" } else { "WARN" },
        detail: if cgroup {
            "cgroup v2 available"
        } else {
            "resource limits unavailable"
        }
        .into(),
        action: (!cgroup).then_some("Enable cgroup v2 for hard worker memory limits."),
    });
    let ready = !checks.iter().any(|check| check.status == "FAIL");
    DoctorReport {
        ready,
        omarag_version: version,
        checks,
    }
}

fn memory_info() -> (u64, u64) {
    let mut total = 0;
    let mut available = 0;
    if let Ok(content) = std::fs::read_to_string("/proc/meminfo") {
        for line in content.lines() {
            let value = line
                .split_whitespace()
                .nth(1)
                .and_then(|value| value.parse::<u64>().ok())
                .unwrap_or_default()
                * 1024;
            if line.starts_with("MemTotal:") {
                total = value;
            } else if line.starts_with("MemAvailable:") {
                available = value;
            }
        }
    }
    (total, available)
}

fn print_json<T: Serialize>(value: &T) -> Result<()> {
    println!(
        "{}",
        serde_json::to_string_pretty(value).context("Could not serialize JSON output")?
    );
    Ok(())
}

fn output<T: Serialize>(value: &T, json: bool, human: impl FnOnce() -> String) -> Result<()> {
    if json {
        print_json(value)
    } else {
        println!("{}", human());
        Ok(())
    }
}
