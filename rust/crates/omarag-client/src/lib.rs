use async_trait::async_trait;
use futures_util::{StreamExt, stream::BoxStream};
use omarag_domain::{
    ApiErrorResponse, BackendMeta, BackupSummary, CommitImportRequest, ConfigDocument,
    CreateSource, CreateWorkspace, DocumentSummary, DomainEvent, EventSubscription, HealthReport,
    IdempotentResult, ImportPreflightBatch, IngestRequest, JobId, JobSnapshot, OmaRagError,
    OmaResult, PreflightImportRequest, QualityReport, RetrievalExplanation, RunId, RunRequest,
    RunSnapshot, SearchHit, SearchRequest, SourceDefinition, UpdateConfig, WorkspaceId,
    WorkspaceManifest, WorkspaceSummary,
};
use reqwest::{Method, RequestBuilder, Response, StatusCode};
use reqwest_eventsource::{Event, EventSource};
use std::sync::{Arc, RwLock};
use url::Url;

#[async_trait]
pub trait OmaRagClient: Send + Sync {
    async fn meta(&self) -> OmaResult<BackendMeta>;
    async fn health(&self) -> OmaResult<HealthReport>;
    async fn model_runtime(&self, workspace: Option<WorkspaceId>) -> OmaResult<serde_json::Value> {
        let _ = workspace;
        Err(OmaRagError::Protocol("Model runtime is unavailable".into()))
    }
    async fn system_dependencies(&self) -> OmaResult<serde_json::Value> {
        Err(OmaRagError::Protocol(
            "Dependency report is unavailable".into(),
        ))
    }
    async fn list_workspaces(&self) -> OmaResult<Vec<WorkspaceSummary>>;
    async fn open_workspace(&self, id: WorkspaceId) -> OmaResult<WorkspaceManifest>;
    async fn create_workspace(&self, request: CreateWorkspace) -> OmaResult<WorkspaceManifest>;
    async fn delete_workspace(&self, id: WorkspaceId, physical: bool) -> OmaResult<()>;
    async fn list_documents(&self, workspace: WorkspaceId) -> OmaResult<Vec<DocumentSummary>>;
    async fn delete_document(&self, workspace: WorkspaceId, document_id: String) -> OmaResult<()>;
    async fn restore_document(&self, workspace: WorkspaceId, document_id: String) -> OmaResult<()>;
    async fn list_sources(&self, workspace: WorkspaceId) -> OmaResult<Vec<SourceDefinition>>;
    async fn create_source(
        &self,
        workspace: WorkspaceId,
        request: CreateSource,
    ) -> OmaResult<SourceDefinition>;
    async fn quality(&self, workspace: WorkspaceId) -> OmaResult<QualityReport>;
    async fn config(&self, workspace: WorkspaceId) -> OmaResult<ConfigDocument>;
    async fn update_config(
        &self,
        workspace: WorkspaceId,
        request: UpdateConfig,
        etag: String,
    ) -> OmaResult<ConfigDocument>;
    async fn list_backups(&self, workspace: WorkspaceId) -> OmaResult<Vec<BackupSummary>>;
    async fn create_backup(&self, workspace: WorkspaceId) -> OmaResult<BackupSummary>;
    async fn list_jobs(&self, workspace: Option<WorkspaceId>) -> OmaResult<Vec<JobSnapshot>>;
    async fn ingest(
        &self,
        workspace: WorkspaceId,
        request: IngestRequest,
        idempotency_key: String,
    ) -> OmaResult<IdempotentResult>;
    async fn preflight_import(
        &self,
        workspace: WorkspaceId,
        request: PreflightImportRequest,
    ) -> OmaResult<ImportPreflightBatch>;
    async fn commit_import(
        &self,
        workspace: WorkspaceId,
        request: CommitImportRequest,
        idempotency_key: String,
    ) -> OmaResult<IdempotentResult>;
    async fn search(
        &self,
        workspace: WorkspaceId,
        request: SearchRequest,
    ) -> OmaResult<Vec<SearchHit>>;
    async fn explain_search(
        &self,
        workspace: WorkspaceId,
        request: SearchRequest,
    ) -> OmaResult<RetrievalExplanation>;
    async fn citation_preview(
        &self,
        workspace: WorkspaceId,
        run_id: RunId,
        citation_index: usize,
        max_px: u32,
    ) -> OmaResult<Vec<u8>>;
    async fn start_run(
        &self,
        workspace: WorkspaceId,
        request: RunRequest,
    ) -> OmaResult<RunSnapshot>;
    async fn get_run(&self, run_id: RunId) -> OmaResult<RunSnapshot>;
    async fn cancel_run(&self, run_id: RunId) -> OmaResult<RunSnapshot>;
    async fn job_snapshot(&self, job_id: JobId) -> OmaResult<JobSnapshot>;
    async fn pause_job(&self, job_id: JobId) -> OmaResult<JobSnapshot>;
    async fn resume_job(&self, job_id: JobId) -> OmaResult<JobSnapshot>;
    async fn cancel_job(&self, job_id: JobId) -> OmaResult<JobSnapshot>;
    async fn subscribe_events(
        &self,
        request: EventSubscription,
    ) -> OmaResult<BoxStream<'static, OmaResult<DomainEvent>>>;
}

#[derive(Debug, Clone)]
pub struct HttpOmaRagClient {
    base_url: Url,
    token: Option<String>,
    http: reqwest::Client,
}

impl HttpOmaRagClient {
    pub fn new(base_url: Url, token: Option<String>) -> OmaResult<Self> {
        let http = reqwest::Client::builder()
            .user_agent(concat!("omarag-client/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|error| OmaRagError::Transport(error.to_string()))?;
        Ok(Self {
            base_url,
            token,
            http,
        })
    }

    fn url(&self, path: &str) -> OmaResult<Url> {
        self.base_url
            .join(path.trim_start_matches('/'))
            .map_err(|error| OmaRagError::Protocol(error.to_string()))
    }

    fn request(&self, method: Method, path: &str) -> OmaResult<RequestBuilder> {
        let mut request = self.http.request(method, self.url(path)?);
        if let Some(token) = &self.token {
            request = request.bearer_auth(token);
        }
        Ok(request)
    }

    async fn json<T: serde::de::DeserializeOwned>(&self, response: Response) -> OmaResult<T> {
        if response.status().is_success() {
            return response
                .json::<T>()
                .await
                .map_err(|error| OmaRagError::Protocol(error.to_string()));
        }
        Err(Self::api_error(response).await)
    }

    async fn api_error(response: Response) -> OmaRagError {
        let status = response.status().as_u16();
        match response.json::<ApiErrorResponse>().await {
            Ok(body) => OmaRagError::Api {
                status,
                code: body.error.code,
                message: body.error.message,
                retryable: body.error.retryable,
            },
            Err(error) => OmaRagError::Protocol(format!(
                "HTTP {status} without a valid error object: {error}"
            )),
        }
    }

    async fn send_json<B, T>(&self, method: Method, path: &str, body: &B) -> OmaResult<T>
    where
        B: serde::Serialize + Sync,
        T: serde::de::DeserializeOwned,
    {
        let response = self
            .request(method, path)?
            .json(body)
            .send()
            .await
            .map_err(|error| OmaRagError::Transport(error.to_string()))?;
        self.json(response).await
    }

    async fn send_empty<T>(&self, method: Method, path: &str) -> OmaResult<T>
    where
        T: serde::de::DeserializeOwned,
    {
        let response = self
            .request(method, path)?
            .send()
            .await
            .map_err(|error| OmaRagError::Transport(error.to_string()))?;
        self.json(response).await
    }

    async fn send_no_content(&self, method: Method, path: &str) -> OmaResult<()> {
        let response = self
            .request(method, path)?
            .send()
            .await
            .map_err(|error| OmaRagError::Transport(error.to_string()))?;
        if response.status().is_success() {
            Ok(())
        } else {
            Err(Self::api_error(response).await)
        }
    }
}

#[async_trait]
impl OmaRagClient for HttpOmaRagClient {
    async fn meta(&self) -> OmaResult<BackendMeta> {
        self.send_empty(Method::GET, "/v1/meta").await
    }

    async fn health(&self) -> OmaResult<HealthReport> {
        self.send_empty(Method::GET, "/v1/health").await
    }

    async fn model_runtime(&self, workspace: Option<WorkspaceId>) -> OmaResult<serde_json::Value> {
        let path = workspace.map_or_else(
            || "/v1/models/runtime".into(),
            |workspace| format!("/v1/models/runtime?workspace_id={workspace}"),
        );
        self.send_empty(Method::GET, &path).await
    }

    async fn system_dependencies(&self) -> OmaResult<serde_json::Value> {
        self.send_empty(Method::GET, "/v1/system/dependencies")
            .await
    }

    async fn list_workspaces(&self) -> OmaResult<Vec<WorkspaceSummary>> {
        self.send_empty(Method::GET, "/v1/workspaces").await
    }

    async fn open_workspace(&self, id: WorkspaceId) -> OmaResult<WorkspaceManifest> {
        self.send_empty(Method::POST, &format!("/v1/workspaces/{id}/open"))
            .await
    }

    async fn create_workspace(&self, request: CreateWorkspace) -> OmaResult<WorkspaceManifest> {
        self.send_json(Method::POST, "/v1/workspaces", &request)
            .await
    }

    async fn delete_workspace(&self, id: WorkspaceId, physical: bool) -> OmaResult<()> {
        let response = self
            .request(Method::DELETE, &format!("/v1/workspaces/{id}"))?
            .json(&serde_json::json!({
                "confirm": "DELETE",
                "mode": if physical { "physical" } else { "unregister" },
            }))
            .send()
            .await
            .map_err(|error| OmaRagError::Transport(error.to_string()))?;
        if response.status().is_success() {
            Ok(())
        } else {
            Err(Self::api_error(response).await)
        }
    }

    async fn list_documents(&self, workspace: WorkspaceId) -> OmaResult<Vec<DocumentSummary>> {
        self.send_empty(
            Method::GET,
            &format!("/v1/workspaces/{workspace}/documents"),
        )
        .await
    }

    async fn delete_document(&self, workspace: WorkspaceId, document_id: String) -> OmaResult<()> {
        self.send_no_content(
            Method::DELETE,
            &format!("/v1/workspaces/{workspace}/documents/{document_id}"),
        )
        .await
    }

    async fn restore_document(&self, workspace: WorkspaceId, document_id: String) -> OmaResult<()> {
        self.send_no_content(
            Method::POST,
            &format!("/v1/workspaces/{workspace}/documents/{document_id}/restore"),
        )
        .await
    }

    async fn list_sources(&self, workspace: WorkspaceId) -> OmaResult<Vec<SourceDefinition>> {
        self.send_empty(Method::GET, &format!("/v1/workspaces/{workspace}/sources"))
            .await
    }

    async fn create_source(
        &self,
        workspace: WorkspaceId,
        request: CreateSource,
    ) -> OmaResult<SourceDefinition> {
        self.send_json(
            Method::POST,
            &format!("/v1/workspaces/{workspace}/sources"),
            &request,
        )
        .await
    }

    async fn quality(&self, workspace: WorkspaceId) -> OmaResult<QualityReport> {
        self.send_empty(Method::GET, &format!("/v1/workspaces/{workspace}/quality"))
            .await
    }

    async fn config(&self, workspace: WorkspaceId) -> OmaResult<ConfigDocument> {
        self.send_empty(Method::GET, &format!("/v1/workspaces/{workspace}/config"))
            .await
    }

    async fn update_config(
        &self,
        workspace: WorkspaceId,
        request: UpdateConfig,
        etag: String,
    ) -> OmaResult<ConfigDocument> {
        let response = self
            .request(Method::PUT, &format!("/v1/workspaces/{workspace}/config"))?
            .header("If-Match", etag)
            .json(&request)
            .send()
            .await
            .map_err(|error| OmaRagError::Transport(error.to_string()))?;
        self.json(response).await
    }

    async fn list_backups(&self, workspace: WorkspaceId) -> OmaResult<Vec<BackupSummary>> {
        self.send_empty(Method::GET, &format!("/v1/workspaces/{workspace}/backups"))
            .await
    }

    async fn create_backup(&self, workspace: WorkspaceId) -> OmaResult<BackupSummary> {
        self.send_empty(Method::POST, &format!("/v1/workspaces/{workspace}/backups"))
            .await
    }

    async fn list_jobs(&self, workspace: Option<WorkspaceId>) -> OmaResult<Vec<JobSnapshot>> {
        let path = workspace.map_or_else(
            || "/v1/jobs".to_owned(),
            |id| format!("/v1/jobs?workspace_id={id}"),
        );
        self.send_empty(Method::GET, &path).await
    }

    async fn ingest(
        &self,
        workspace: WorkspaceId,
        request: IngestRequest,
        idempotency_key: String,
    ) -> OmaResult<IdempotentResult> {
        let response = self
            .request(
                Method::POST,
                &format!("/v1/workspaces/{workspace}/documents/ingest"),
            )?
            .header("Idempotency-Key", idempotency_key)
            .json(&request)
            .send()
            .await
            .map_err(|error| OmaRagError::Transport(error.to_string()))?;
        self.json(response).await
    }

    async fn preflight_import(
        &self,
        workspace: WorkspaceId,
        request: PreflightImportRequest,
    ) -> OmaResult<ImportPreflightBatch> {
        self.send_json(
            Method::POST,
            &format!("/v1/workspaces/{workspace}/imports/preflight"),
            &request,
        )
        .await
    }

    async fn commit_import(
        &self,
        workspace: WorkspaceId,
        request: CommitImportRequest,
        idempotency_key: String,
    ) -> OmaResult<IdempotentResult> {
        let response = self
            .request(
                Method::POST,
                &format!("/v1/workspaces/{workspace}/imports/commit"),
            )?
            .header("Idempotency-Key", idempotency_key)
            .json(&request)
            .send()
            .await
            .map_err(|error| OmaRagError::Transport(error.to_string()))?;
        self.json(response).await
    }

    async fn search(
        &self,
        workspace: WorkspaceId,
        request: SearchRequest,
    ) -> OmaResult<Vec<SearchHit>> {
        self.send_json(
            Method::POST,
            &format!("/v1/workspaces/{workspace}/search"),
            &request,
        )
        .await
    }

    async fn explain_search(
        &self,
        workspace: WorkspaceId,
        request: SearchRequest,
    ) -> OmaResult<RetrievalExplanation> {
        self.send_json(
            Method::POST,
            &format!("/v1/workspaces/{workspace}/search/explain"),
            &request,
        )
        .await
    }

    async fn citation_preview(
        &self,
        workspace: WorkspaceId,
        run_id: RunId,
        citation_index: usize,
        max_px: u32,
    ) -> OmaResult<Vec<u8>> {
        let response = self
            .request(
                Method::GET,
                &format!(
                    "/v1/workspaces/{workspace}/runs/{run_id}/citations/{citation_index}/preview?max_px={max_px}"
                ),
            )?
            .send()
            .await
            .map_err(|error| OmaRagError::Transport(error.to_string()))?;
        if !response.status().is_success() {
            return Err(Self::api_error(response).await);
        }
        response
            .bytes()
            .await
            .map(|bytes| bytes.to_vec())
            .map_err(|error| OmaRagError::Transport(error.to_string()))
    }

    async fn start_run(
        &self,
        workspace: WorkspaceId,
        request: RunRequest,
    ) -> OmaResult<RunSnapshot> {
        self.send_json(
            Method::POST,
            &format!("/v1/workspaces/{workspace}/runs"),
            &request,
        )
        .await
    }

    async fn cancel_run(&self, run_id: RunId) -> OmaResult<RunSnapshot> {
        self.send_empty(Method::DELETE, &format!("/v1/runs/{run_id}"))
            .await
    }

    async fn get_run(&self, run_id: RunId) -> OmaResult<RunSnapshot> {
        self.send_empty(Method::GET, &format!("/v1/runs/{run_id}"))
            .await
    }

    async fn job_snapshot(&self, job_id: JobId) -> OmaResult<JobSnapshot> {
        self.send_empty(Method::GET, &format!("/v1/jobs/{job_id}/snapshot"))
            .await
    }

    async fn pause_job(&self, job_id: JobId) -> OmaResult<JobSnapshot> {
        self.send_empty(Method::POST, &format!("/v1/jobs/{job_id}/pause"))
            .await
    }

    async fn resume_job(&self, job_id: JobId) -> OmaResult<JobSnapshot> {
        self.send_empty(Method::POST, &format!("/v1/jobs/{job_id}/resume"))
            .await
    }

    async fn cancel_job(&self, job_id: JobId) -> OmaResult<JobSnapshot> {
        self.send_empty(Method::DELETE, &format!("/v1/jobs/{job_id}"))
            .await
    }

    async fn subscribe_events(
        &self,
        subscription: EventSubscription,
    ) -> OmaResult<BoxStream<'static, OmaResult<DomainEvent>>> {
        let path = if let Some(job_id) = &subscription.job_id {
            format!("/v1/jobs/{job_id}/events")
        } else if let Some(run_id) = &subscription.run_id {
            format!("/v1/runs/{run_id}/events")
        } else if let Some(workspace_id) = &subscription.workspace_id {
            format!("/v1/workspaces/{workspace_id}/events")
        } else {
            "/v1/events".to_owned()
        };
        let mut request = self.request(Method::GET, &path)?;
        if let Some(last_event_id) = subscription.last_event_id {
            request = request.header("Last-Event-ID", last_event_id.to_string());
        }
        let mut source =
            EventSource::new(request).map_err(|error| OmaRagError::Transport(error.to_string()))?;
        let stream = async_stream::stream! {
            while let Some(item) = source.next().await {
                match item {
                    Ok(Event::Open) => {}
                    Ok(Event::Message(message)) => {
                        yield serde_json::from_str::<DomainEvent>(&message.data)
                            .map_err(|error| OmaRagError::Protocol(error.to_string()));
                    }
                    Err(error) => {
                        yield Err(OmaRagError::Transport(error.to_string()));
                        if matches!(
                            error,
                            reqwest_eventsource::Error::InvalidStatusCode(
                                StatusCode::UNAUTHORIZED,
                                _
                            )
                        ) {
                            break;
                        }
                    }
                }
            }
            source.close();
        };
        Ok(Box::pin(stream))
    }
}

#[derive(Debug, Clone, Default)]
pub struct MockOmaRagClient {
    state: Arc<RwLock<MockState>>,
}

#[derive(Debug, Default)]
struct MockState {
    meta: Option<BackendMeta>,
    workspaces: Vec<WorkspaceManifest>,
    jobs: Vec<JobSnapshot>,
    events: Vec<DomainEvent>,
}

impl MockOmaRagClient {
    pub fn with_meta(meta: BackendMeta) -> Self {
        let result = Self::default();
        result.state.write().expect("mock lock").meta = Some(meta);
        result
    }

    pub fn push_event(&self, event: DomainEvent) {
        self.state.write().expect("mock lock").events.push(event);
    }

    pub fn push_workspace(&self, workspace: WorkspaceManifest) {
        self.state
            .write()
            .expect("mock lock")
            .workspaces
            .push(workspace);
    }
}

#[async_trait]
impl OmaRagClient for MockOmaRagClient {
    async fn meta(&self) -> OmaResult<BackendMeta> {
        self.state
            .read()
            .expect("mock lock")
            .meta
            .clone()
            .ok_or_else(|| OmaRagError::Protocol("Mock-Metadaten fehlen".into()))
    }

    async fn health(&self) -> OmaResult<HealthReport> {
        Ok(HealthReport {
            status: "ok".into(),
            ready: true,
            checks: Default::default(),
        })
    }

    async fn list_workspaces(&self) -> OmaResult<Vec<WorkspaceSummary>> {
        Ok(self
            .state
            .read()
            .expect("mock lock")
            .workspaces
            .iter()
            .map(|item| WorkspaceSummary {
                id: item.id.clone(),
                name: item.name.clone(),
                path: item.path.clone(),
                read_only: item.read_only,
                updated_at: item.updated_at.clone(),
                etag: item.etag.clone(),
            })
            .collect())
    }

    async fn open_workspace(&self, id: WorkspaceId) -> OmaResult<WorkspaceManifest> {
        self.state
            .read()
            .expect("mock lock")
            .workspaces
            .iter()
            .find(|item| item.id == id)
            .cloned()
            .ok_or_else(|| OmaRagError::Protocol("Mock-Workspace fehlt".into()))
    }

    async fn create_workspace(&self, _: CreateWorkspace) -> OmaResult<WorkspaceManifest> {
        Err(OmaRagError::Protocol("Nicht im Mock konfiguriert".into()))
    }

    async fn preflight_import(
        &self,
        _: WorkspaceId,
        _: PreflightImportRequest,
    ) -> OmaResult<ImportPreflightBatch> {
        Err(OmaRagError::Protocol("Nicht im Mock konfiguriert".into()))
    }

    async fn commit_import(
        &self,
        _: WorkspaceId,
        _: CommitImportRequest,
        _: String,
    ) -> OmaResult<IdempotentResult> {
        Err(OmaRagError::Protocol("Nicht im Mock konfiguriert".into()))
    }

    async fn delete_workspace(&self, id: WorkspaceId, _: bool) -> OmaResult<()> {
        self.state
            .write()
            .expect("mock lock")
            .workspaces
            .retain(|workspace| workspace.id != id);
        Ok(())
    }

    async fn list_documents(&self, _: WorkspaceId) -> OmaResult<Vec<DocumentSummary>> {
        Ok(Vec::new())
    }

    async fn delete_document(&self, _: WorkspaceId, _: String) -> OmaResult<()> {
        Ok(())
    }

    async fn restore_document(&self, _: WorkspaceId, _: String) -> OmaResult<()> {
        Ok(())
    }

    async fn list_sources(&self, _: WorkspaceId) -> OmaResult<Vec<SourceDefinition>> {
        Ok(Vec::new())
    }

    async fn create_source(&self, _: WorkspaceId, _: CreateSource) -> OmaResult<SourceDefinition> {
        Err(OmaRagError::Protocol("Nicht im Mock konfiguriert".into()))
    }

    async fn quality(&self, workspace: WorkspaceId) -> OmaResult<QualityReport> {
        Ok(QualityReport {
            workspace_id: workspace,
            status: "ok".into(),
            document_count: 0,
            completed_imports: 0,
            failed_jobs: 0,
            issues: Vec::new(),
            latest_evaluation_id: None,
            retrieval_metrics: Default::default(),
            generated_at: "now".into(),
        })
    }

    async fn config(&self, _: WorkspaceId) -> OmaResult<ConfigDocument> {
        Ok(ConfigDocument {
            content: String::new(),
            etag: String::new(),
        })
    }

    async fn update_config(
        &self,
        _: WorkspaceId,
        _: UpdateConfig,
        _: String,
    ) -> OmaResult<ConfigDocument> {
        Err(OmaRagError::Protocol("Nicht im Mock konfiguriert".into()))
    }

    async fn list_backups(&self, _: WorkspaceId) -> OmaResult<Vec<BackupSummary>> {
        Ok(Vec::new())
    }

    async fn create_backup(&self, _: WorkspaceId) -> OmaResult<BackupSummary> {
        Err(OmaRagError::Protocol("Nicht im Mock konfiguriert".into()))
    }

    async fn list_jobs(&self, workspace: Option<WorkspaceId>) -> OmaResult<Vec<JobSnapshot>> {
        Ok(self
            .state
            .read()
            .expect("mock lock")
            .jobs
            .iter()
            .filter(|job| workspace.as_ref().is_none_or(|id| job.workspace_id == *id))
            .cloned()
            .collect())
    }

    async fn ingest(
        &self,
        _: WorkspaceId,
        _: IngestRequest,
        _: String,
    ) -> OmaResult<IdempotentResult> {
        Err(OmaRagError::Protocol("Nicht im Mock konfiguriert".into()))
    }

    async fn search(&self, _: WorkspaceId, _: SearchRequest) -> OmaResult<Vec<SearchHit>> {
        Ok(Vec::new())
    }

    async fn explain_search(
        &self,
        _: WorkspaceId,
        request: SearchRequest,
    ) -> OmaResult<RetrievalExplanation> {
        Ok(RetrievalExplanation {
            query: request.query,
            candidates: Vec::new(),
            ranked: Vec::new(),
            timing: omarag_domain::RetrievalTiming {
                search_ms: 0.0,
                total_ms: 0.0,
            },
            provider_notes: Vec::new(),
        })
    }

    async fn citation_preview(
        &self,
        _: WorkspaceId,
        _: RunId,
        _: usize,
        _: u32,
    ) -> OmaResult<Vec<u8>> {
        Err(OmaRagError::Protocol("Nicht im Mock konfiguriert".into()))
    }

    async fn start_run(&self, _: WorkspaceId, _: RunRequest) -> OmaResult<RunSnapshot> {
        Err(OmaRagError::Protocol("Nicht im Mock konfiguriert".into()))
    }

    async fn cancel_run(&self, _: RunId) -> OmaResult<RunSnapshot> {
        Err(OmaRagError::Protocol("Nicht im Mock konfiguriert".into()))
    }

    async fn get_run(&self, _: RunId) -> OmaResult<RunSnapshot> {
        Err(OmaRagError::Protocol("Nicht im Mock konfiguriert".into()))
    }

    async fn job_snapshot(&self, job_id: JobId) -> OmaResult<JobSnapshot> {
        self.state
            .read()
            .expect("mock lock")
            .jobs
            .iter()
            .find(|item| item.id == job_id)
            .cloned()
            .ok_or_else(|| OmaRagError::Protocol("Mock-Job fehlt".into()))
    }

    async fn pause_job(&self, job_id: JobId) -> OmaResult<JobSnapshot> {
        self.job_snapshot(job_id).await
    }

    async fn resume_job(&self, job_id: JobId) -> OmaResult<JobSnapshot> {
        self.job_snapshot(job_id).await
    }

    async fn cancel_job(&self, job_id: JobId) -> OmaResult<JobSnapshot> {
        self.job_snapshot(job_id).await
    }

    async fn subscribe_events(
        &self,
        request: EventSubscription,
    ) -> OmaResult<BoxStream<'static, OmaResult<DomainEvent>>> {
        let events = self
            .state
            .read()
            .expect("mock lock")
            .events
            .iter()
            .filter(|event| {
                event.event_id > request.last_event_id.unwrap_or(0)
                    && request
                        .workspace_id
                        .as_ref()
                        .is_none_or(|id| event.workspace_id.as_ref() == Some(id))
                    && request
                        .job_id
                        .as_ref()
                        .is_none_or(|id| event.job_id.as_ref() == Some(id))
                    && request
                        .run_id
                        .as_ref()
                        .is_none_or(|id| event.run_id.as_ref() == Some(id))
            })
            .cloned()
            .map(Ok)
            .collect::<Vec<_>>();
        Ok(Box::pin(futures_util::stream::iter(events)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures_util::StreamExt;
    use omarag_domain::CapabilitySet;
    use serde_json::json;

    fn meta() -> BackendMeta {
        BackendMeta {
            api_version: "1.0".into(),
            min_client_version: "1.0".into(),
            max_client_version: "1.x".into(),
            omarag_version: "1.0.0".into(),
            haiku_version: None,
            adapter: None,
            backend_id: "mock".into(),
            capabilities: CapabilitySet::default(),
            deprecations: Vec::new(),
        }
    }

    #[tokio::test]
    async fn mock_replays_only_new_events() {
        let client = MockOmaRagClient::with_meta(meta());
        for event_id in 1..=3 {
            client.push_event(DomainEvent {
                event_id,
                sequence: event_id,
                timestamp: "2026-07-25T00:00:00Z".into(),
                event_type: "job.progress".into(),
                workspace_id: Some("ws-1".into()),
                job_id: Some("job-1".into()),
                run_id: None,
                correlation_id: "c".into(),
                schema_version: 1,
                payload: json!({}),
            });
        }
        let events = client
            .subscribe_events(EventSubscription {
                workspace_id: Some("ws-1".into()),
                job_id: None,
                run_id: None,
                last_event_id: Some(1),
            })
            .await
            .unwrap()
            .collect::<Vec<_>>()
            .await;
        assert_eq!(events.len(), 2);
        assert_eq!(events[0].as_ref().unwrap().event_id, 2);
    }
}
