use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use omarag_client::{HttpOmaRagClient, OmaRagClient};
use omarag_domain::{
    CreateWorkspace, EvidenceMode, IngestRequest, JobStatus, RunRequest, SearchRequest,
};
use serde::Serialize;
use std::time::Duration;
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
                    meta.haiku_version.as_deref().unwrap_or("nicht installiert")
                );
                println!(
                    "RAG bereit: {}",
                    if meta.adapter.is_some() { "ja" } else { "nein" }
                );
            }
        }
        Command::Workspace { command } => match command {
            WorkspaceCommand::List => {
                let workspaces = client.list_workspaces().await?;
                if args.json {
                    print_json(&workspaces)?;
                } else if workspaces.is_empty() {
                    println!("Keine Workspaces.");
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
                    format!("Workspace {} ({}) erstellt", workspace.name, workspace.id)
                })?;
            }
            WorkspaceCommand::Open { id } => {
                let workspace = client.open_workspace(id).await?;
                output(&workspace, args.json, || {
                    format!("Workspace {} geoeffnet", workspace.name)
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
                    "Importjob {}{}",
                    result.id,
                    if result.reused {
                        " (wiederverwendet)"
                    } else {
                        ""
                    }
                )
            })?;
        }
        Command::Jobs { workspace } => {
            let jobs = client.list_jobs(workspace).await?;
            if args.json {
                print_json(&jobs)?;
            } else if jobs.is_empty() {
                println!("Keine Auftraege.");
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
                output(&run, args.json, || format!("Run {} gestartet", run.id))?;
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
                                "[{}] S. {} — {}",
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
                        bail!("Run endete mit {:?}: {:?}", current.status, current.error);
                    }
                    break;
                }
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
        }
    }
    Ok(())
}

fn print_json<T: Serialize>(value: &T) -> Result<()> {
    println!(
        "{}",
        serde_json::to_string_pretty(value).context("JSON-Ausgabe fehlgeschlagen")?
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
