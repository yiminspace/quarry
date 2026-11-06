//! Shared connection selection for Quarry and hosts that embed the CLI.
use crate::{
    federation::{Federation, ProviderStatus, Resource, Status},
    http::{CloudBackend, NativeHistory},
};
use quarry_core::{Engine, QueryRequest};
use quarry_runtime::ConnectionRecord;
use quarry_service::{Failure, QuarryService, Result};
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, clap::ValueEnum)]
pub enum Source {
    Local,
    Cloud,
}

impl Source {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Local => "local",
            Self::Cloud => "cloud",
        }
    }
}

pub struct Selector<'a> {
    pub target: &'a str,
    pub source: Option<Source>,
    pub env: Option<&'a str>,
    pub workspace: Option<&'a Path>,
}

#[derive(Clone)]
pub enum Target {
    Local {
        id: String,
        record: ConnectionRecord,
    },
    Cloud(Resource),
}

pub fn local_target_id(record: &ConnectionRecord) -> String {
    record.target_id()
}

pub fn resolve_local_target(host: &QuarryService, id: &str) -> Result<ConnectionRecord> {
    host.catalog()?
        .connections()?
        .into_iter()
        .find(|record| local_target_id(record) == id)
        .ok_or_else(|| not_found(id))
}

impl Target {
    pub fn local(record: ConnectionRecord) -> Self {
        Self::Local {
            id: local_target_id(&record),
            record,
        }
    }

    pub fn id(&self) -> &str {
        match self {
            Self::Local { id, .. } => id,
            Self::Cloud(resource) => &resource.id,
        }
    }

    pub fn db(&self) -> &str {
        match self {
            Self::Local { record, .. } => record.logical_db(),
            Self::Cloud(resource) => &resource.db,
        }
    }

    pub fn env(&self) -> Option<&str> {
        match self {
            Self::Local { record, .. } => record.display_env(),
            Self::Cloud(resource) => resource.env.as_deref(),
        }
    }

    pub fn source(&self) -> Source {
        match self {
            Self::Local { .. } => Source::Local,
            Self::Cloud(_) => Source::Cloud,
        }
    }

    pub(crate) fn supports_saved_query(
        &self,
        host: &QuarryService,
        db: &str,
        workspace: &Path,
    ) -> Result<bool> {
        if self.db() == db {
            return Ok(true);
        }
        match self {
            Self::Local { record, .. } => Ok(record.key == db),
            Self::Cloud(resource) => {
                let Some(association) = &resource.association else {
                    return Ok(false);
                };
                match resolve_local_target(host, &association.id) {
                    Ok(record) => Ok(owns_saved_query(&record, db, workspace)),
                    Err(error) if error.code == "not_found" => Ok(false),
                    Err(error) => Err(error),
                }
            }
        }
    }

    pub fn engine(&self) -> Result<Engine> {
        match self {
            Self::Local { record, .. } => Ok(record.spec()?.engine),
            Self::Cloud(resource) => Ok(resource.engine),
        }
    }

    pub fn as_json(&self) -> Value {
        match self {
            Self::Local { id, record } => json!({
                "id": id, "resourceId": id, "source": "local", "status": "configured",
                "key": record.key, "db": record.logical_db(), "env": record.display_env(),
                "workspace": record.workspace, "project": record.group,
                "production": record.production, "engine": self.engine().ok(),
            }),
            Self::Cloud(resource) => json!({
                "id": resource.id, "resourceId": resource.id, "source": "cloud", "status": resource.status,
                "upstreamId": resource.upstream_id, "provider": resource.provider,
                "project": resource.project, "db": resource.db, "env": resource.env,
                "engine": resource.engine, "production": resource.production,
                "association": resource.association,
            }),
        }
    }

    pub fn history_target(&self) -> Value {
        match self {
            Self::Local { record, .. } => json!({
                "db":record.logical_db(), "env":record.display_env(), "placement":"local",
                "workspace":record.workspace, "key":record.key, "project":record.group,
            }),
            Self::Cloud(resource) => json!({
                "db":resource.db, "env":resource.env, "placement":"remote", "project":resource.project,
            }),
        }
    }

    pub async fn record_history(
        &self,
        history: &NativeHistory,
        sql: &str,
        request_id: &str,
    ) -> Result<()> {
        history.append_entry(json!({
            "id":request_id, "resourceId":self.id(), "sql":sql, "target":self.history_target(),
            "createdAt":SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64,
        })).await
    }

    pub async fn execute(
        &self,
        host: &QuarryService,
        federation: Option<&Federation>,
        sql: &str,
        max_rows: usize,
        offset: usize,
        request_id: &str,
    ) -> Result<Value> {
        if !(1..=5000).contains(&max_rows) {
            return Err(Failure::invalid("max-rows must be between 1 and 5000"));
        }
        let offset = offset.min(1_000_000);
        match self {
            Self::Local { record, id } => {
                let mut request = QueryRequest::new(sql);
                request.policy.max_rows = Some(max_rows);
                request.policy.offset = offset;
                request.policy.max_bytes = 16 * 1024 * 1024;
                let cancellation = request.cancellation.clone();
                let interrupt = tokio::spawn(async move {
                    if tokio::signal::ctrl_c().await.is_ok() {
                        cancellation.cancel();
                    }
                });
                let result = host.execute(record, request).await;
                interrupt.abort();
                let mut result = result?.to_legacy_json();
                result["resourceId"] = json!(id);
                result["requestId"] = json!(request_id);
                Ok(result)
            }
            Self::Cloud(resource) => {
                let federation = ready_provider(resource, federation)?;
                federation
                    .request(
                        "POST",
                        "/api/query",
                        &[],
                        Some(json!({
                            "resourceId": resource.id, "sql": sql, "maxRows": max_rows,
                            "offset": offset, "requestId": request_id,
                        })),
                    )
                    .await
            }
        }
    }

    pub async fn tables(
        &self,
        host: &QuarryService,
        federation: Option<&Federation>,
    ) -> Result<Value> {
        match self {
            Self::Local { record, .. } => host.tables(record).await,
            Self::Cloud(resource) => cloud_get(resource, federation, "/api/tables", vec![]).await,
        }
    }

    pub async fn columns(
        &self,
        host: &QuarryService,
        federation: Option<&Federation>,
        table: &str,
    ) -> Result<Value> {
        match self {
            Self::Local { record, .. } => host.columns(record, table).await,
            Self::Cloud(resource) => {
                cloud_get(
                    resource,
                    federation,
                    "/api/columns",
                    vec![("table", table.to_owned())],
                )
                .await
            }
        }
    }

    pub async fn schema(
        &self,
        host: &QuarryService,
        federation: Option<&Federation>,
    ) -> Result<Value> {
        match self {
            Self::Local { record, id } => {
                let schema = host.local()?.export_schema(record, &host.tunnels).await?;
                Ok(json!({"schema":schema,"engine":"postgres","resourceId":id}))
            }
            Self::Cloud(resource) => {
                cloud_get(resource, federation, "/api/schema/export", vec![]).await
            }
        }
    }

    pub(crate) async fn health(
        &self,
        host: &QuarryService,
        federation: Option<&Federation>,
        timeout: Option<u64>,
    ) -> Result<Value> {
        match self {
            Self::Local { record, .. } => {
                host.metadata(
                    record,
                    quarry_service::MetadataOperation::Ping,
                    timeout,
                    quarry_core::CancellationToken::new(),
                )
                .await
            }
            Self::Cloud(resource) => {
                let request = cloud_get(resource, federation, "/api/health", vec![]);
                match timeout {
                    Some(ms) => tokio::time::timeout(std::time::Duration::from_millis(ms), request)
                        .await
                        .map_err(|_| Failure {
                            status: 504,
                            code: "timeout".into(),
                            message: "Cloud database probe timed out".into(),
                        })?,
                    None => request.await,
                }
            }
        }
    }
}

fn ready_provider<'a>(
    resource: &Resource,
    federation: Option<&'a Federation>,
) -> Result<&'a Federation> {
    federation
        .filter(|_| resource.status == Status::Connected)
        .ok_or_else(|| unavailable(&resource.id, Some(resource.status)))
}

fn owns_saved_query(record: &ConnectionRecord, db: &str, workspace: &Path) -> bool {
    record.workspace == workspace && (record.logical_db() == db || record.key == db)
}

async fn cloud_get(
    resource: &Resource,
    federation: Option<&Federation>,
    path: &str,
    mut pairs: Vec<(&str, String)>,
) -> Result<Value> {
    pairs.push(("resourceId", resource.id.clone()));
    ready_provider(resource, federation)?
        .request("GET", path, &pairs, None)
        .await
}

pub struct Catalog {
    resources: Vec<Target>,
    status: Option<Status>,
    providers: Vec<ProviderStatus>,
}

impl Catalog {
    pub async fn new(host: &QuarryService, federation: Option<&Federation>) -> Result<Self> {
        Self::from_local(host.catalog()?.connections()?, federation).await
    }

    pub async fn cloud_only(federation: &Federation) -> Result<Self> {
        Self::from_local(vec![], Some(federation)).await
    }

    pub(crate) async fn for_saved(
        host: &QuarryService,
        federation: &Federation,
        db: &str,
        env: Option<&str>,
        workspace: &Path,
        source: Option<Source>,
    ) -> Result<Self> {
        let local = if source == Some(Source::Cloud) {
            vec![]
        } else {
            match host.resolve_related(db, env, workspace) {
                Ok(record) => vec![record],
                Err(error) if error.code == "not_found" => vec![],
                Err(error) => return Err(error),
            }
        };
        let mut catalog = Self::from_local(local, Some(federation)).await?;
        if source != Some(Source::Cloud) {
            // The saved file's owning workspace is authoritative. A cloud
            // target with the same display name alone cannot inherit the file.
            let owners: HashSet<_> = host
                .catalog()?
                .connections()?
                .into_iter()
                .filter(|record| owns_saved_query(record, db, workspace))
                .map(|record| record.target_id())
                .collect();
            catalog.resources.retain(|target| match target {
                Target::Local { .. } => true,
                Target::Cloud(resource) => resource
                    .association
                    .as_ref()
                    .is_some_and(|association| owners.contains(&association.id)),
            });
        }
        Ok(catalog)
    }

    async fn from_local(
        local: Vec<ConnectionRecord>,
        federation: Option<&Federation>,
    ) -> Result<Self> {
        let cloud = match federation {
            Some(federation) => Some(federation.catalog().await?),
            None => None,
        };
        let mut resources: Vec<_> = local.into_iter().map(Target::local).collect();
        let (status, providers) = match cloud {
            Some(catalog) => {
                resources.extend(catalog.resources.into_iter().map(Target::Cloud));
                (Some(catalog.status), catalog.providers)
            }
            None => (None, vec![]),
        };
        Ok(Self {
            resources,
            status,
            providers,
        })
    }

    pub fn resources(&self) -> &[Target] {
        &self.resources
    }

    pub fn cloud_status(&self) -> Option<Status> {
        self.status
    }

    pub fn select(&self, selector: &Selector<'_>) -> Result<&Target> {
        let workspace = selector
            .workspace
            .map(|path| quarry_runtime::absolute(path, &std::env::current_dir()?))
            .transpose()?;
        let in_scope = |target: &&Target| {
            if selector
                .source
                .is_some_and(|source| target.source() != source)
            {
                return false;
            }
            match target {
                Target::Local { record, .. } => {
                    workspace
                        .as_ref()
                        .is_none_or(|path| path == &record.workspace)
                        && selector.env.is_none_or(|env| env == record.env)
                }
                Target::Cloud(_) => selector.env.is_none_or(|env| target.env() == Some(env)),
            }
        };
        if let Some(exact) = self
            .resources
            .iter()
            .find(|target| target.id() == selector.target)
        {
            return in_scope(&exact)
                .then_some(exact)
                .ok_or_else(|| not_found(selector.target));
        }
        // Opaque exact IDs must never fall back to a weak match if they go stale.
        if !is_local_id(selector.target) && !is_cloud_id(selector.target) {
            let candidates: Vec<_> = self
                .resources
                .iter()
                .filter(in_scope)
                .filter(|target| {
                    let key = match target {
                        Target::Local { record, .. } => &record.key,
                        Target::Cloud(resource) => &resource.upstream_id,
                    };
                    key == selector.target || target.db() == selector.target
                })
                .collect();
            match candidates.as_slice() {
                [only] if selector.source.is_none() && self.status == Some(Status::Unavailable) => {
                    return Err(Failure {
                        status: 503,
                        code: "provider_unavailable".into(),
                        message: format!(
                            "Cloud discovery is unavailable. Use --source {} or exact resource ID {} to select this target",
                            only.source().as_str(),
                            only.id()
                        ),
                    });
                }
                [one] => return Ok(one),
                [] => {}
                _ => {
                    let choices = candidates
                        .iter()
                        .map(|target| target.id())
                        .collect::<Vec<_>>()
                        .join(", ");
                    return Err(Failure {
                        status: 409,
                        code: "ambiguous".into(),
                        message: format!(
                            "Database {} matches multiple targets. Use --source, --env, --workspace or an exact resource ID ({choices})",
                            selector.target
                        ),
                    });
                }
            }
        }
        if selector.source != Some(Source::Local)
            && !is_local_id(selector.target)
            && self
                .status
                .is_some_and(|status| status != Status::Connected)
        {
            Err(unavailable(selector.target, self.status))
        } else {
            Err(not_found(selector.target))
        }
    }

    pub async fn connections_json(&self, host: &QuarryService) -> Result<Value> {
        let mut document = host.connections().await?;
        if let Some(status) = self.status {
            document["cloudStatus"] = json!(status);
            document["providers"] = json!(self.providers);
        }
        document["resources"] = json!(
            self.resources
                .iter()
                .map(Target::as_json)
                .collect::<Vec<_>>()
        );
        let groups = document["groups"]
            .as_array_mut()
            .expect("Quarry connection groups");
        for group in groups.iter_mut() {
            for item in group["items"].as_array_mut().into_iter().flatten() {
                for env in item["envs"].as_array_mut().into_iter().flatten() {
                    env["source"] = json!("local");
                    env["resourceId"] = env["targetId"].clone();
                }
            }
        }
        for target in &self.resources {
            let Target::Cloud(resource) = target else {
                continue;
            };
            let mut value = target.as_json();
            // Keep the original hosted API's usable key for existing scripts.
            value["key"] = json!(resource.upstream_id);
            value["ssh"] = json!(false);
            value["proxied"] = json!(false);
            let linked = resource.association.as_ref().and_then(|local| {
                groups
                    .iter_mut()
                    .flat_map(|group| group["items"].as_array_mut().into_iter().flatten())
                    .find(|item| {
                        item["envs"]
                            .as_array()
                            .into_iter()
                            .flatten()
                            .any(|env| env["resourceId"] == local.id)
                    })
            });
            if let Some(item) = linked {
                item["is_env_set"] = json!(true);
                item["envs"]
                    .as_array_mut()
                    .expect("Quarry environments")
                    .push(value);
                continue;
            }
            let group_index = groups.iter().position(|group| group["group"] == resource.project && group["provider"] == value["provider"])
                .unwrap_or_else(|| {
                    groups.push(json!({"group":resource.project,"ws":resource.project,"source":"cloud","provider":resource.provider,"items":[]}));
                    groups.len() - 1
                });
            let items = groups[group_index]["items"]
                .as_array_mut()
                .expect("Quarry connection items");
            let item_index = items.iter().position(|item| item["db"] == resource.db)
                .unwrap_or_else(|| {
                    items.push(json!({"db":resource.db,"engine":resource.engine,"is_env_set":true,"envs":[]}));
                    items.len() - 1
                });
            let item = &mut items[item_index];
            item["is_env_set"] = json!(true);
            item["envs"]
                .as_array_mut()
                .expect("Quarry environments")
                .push(value);
        }
        Ok(document)
    }
}

pub fn is_local_id(target: &str) -> bool {
    target
        .strip_prefix("local:")
        .and_then(|id| id.split_once(':'))
        .is_some_and(|(digest, key)| {
            is_digest(digest) && quarry_runtime::validate_name(key).is_ok()
        })
}

pub fn is_cloud_id(target: &str) -> bool {
    target.strip_prefix("f:").is_some_and(is_digest)
}

fn is_digest(digest: &str) -> bool {
    digest.len() == 64 && digest.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn not_found(target: &str) -> Failure {
    Failure {
        status: 404,
        code: "not_found".into(),
        message: format!("Database target {target} was not found"),
    }
}

fn unavailable(target: &str, status: Option<Status>) -> Failure {
    Failure {
        status: 503,
        code: "provider_unavailable".into(),
        message: format!(
            "Cloud target {target} is unavailable ({})",
            status.map_or("unavailable", |status| status.as_str())
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{federation::ProviderIdentity, local_host};
    use std::{fs, path::PathBuf};

    fn workspace(root: &Path, name: &str, url: &str) -> PathBuf {
        let path = root.join(name);
        fs::create_dir(&path).unwrap();
        fs::write(
            path.join("connections.toml"),
            format!("[notes]\nurl = '{url}'\nenv = 'dev'\n"),
        )
        .unwrap();
        path.canonicalize().unwrap()
    }

    #[tokio::test]
    async fn select_exact_source_and_workspace_without_weak_fallback() {
        let temp = tempfile::tempdir().unwrap();
        let first = workspace(temp.path(), "first", "sqlite:///one.db");
        let second = workspace(temp.path(), "second", "sqlite:///two.db");
        let host = local_host(
            Some(temp.path().join("config.toml")),
            vec![first.clone(), second],
            &[],
        )
        .unwrap();
        let mut catalog = Catalog::new(&host, None).await.unwrap();
        let provider = ProviderIdentity::ylab("https://ylab.example/", "test", "owner").unwrap();
        let id = provider.route_id("notes");
        catalog.resources.push(Target::Cloud(Resource {
            id: id.clone(),
            upstream_id: "notes".into(),
            provider,
            project: "demo".into(),
            db: "notes".into(),
            env: Some("dev".into()),
            engine: Engine::Sqlite,
            production: false,
            status: Status::Connected,
            association: None,
        }));
        catalog.status = Some(Status::Connected);
        let mut selector = Selector {
            target: "notes",
            source: None,
            env: None,
            workspace: None,
        };
        assert_eq!(catalog.select(&selector).err().unwrap().code, "ambiguous");
        selector.source = Some(Source::Cloud);
        assert_eq!(catalog.select(&selector).unwrap().id(), id);
        selector.source = Some(Source::Local);
        assert_eq!(catalog.select(&selector).err().unwrap().code, "ambiguous");
        selector.workspace = Some(&first);
        let local_id = catalog.select(&selector).unwrap().id().to_owned();
        selector.target = &local_id;
        selector.source = None;
        selector.workspace = None;
        assert_eq!(catalog.select(&selector).unwrap().id(), local_id);
        assert_eq!(
            resolve_local_target(&host, &local_id).unwrap().workspace,
            first
        );
        selector.source = Some(Source::Cloud);
        assert_eq!(catalog.select(&selector).err().unwrap().code, "not_found");

        catalog.resources.truncate(1);
        catalog.status = Some(Status::Unavailable);
        selector = Selector {
            target: "notes",
            source: None,
            env: None,
            workspace: None,
        };
        assert_eq!(
            catalog.select(&selector).err().unwrap().code,
            "provider_unavailable"
        );
        selector.source = Some(Source::Local);
        assert_eq!(catalog.select(&selector).unwrap().id(), local_id);
        selector.source = None;
        selector.target = &local_id;
        assert_eq!(catalog.select(&selector).unwrap().id(), local_id);
        selector.target = &id;
        assert_eq!(
            catalog.select(&selector).err().unwrap().code,
            "provider_unavailable"
        );
    }

    #[test]
    fn local_id_tracks_location_and_survives_credential_rotation() {
        let temp = tempfile::tempdir().unwrap();
        let path = workspace(temp.path(), "first", "postgres://user:old@db.example/notes");
        let host = local_host(Some(temp.path().join("config.toml")), vec![path], &[]).unwrap();
        let mut record = host.resolve("notes", None, None).unwrap();
        let id = record.target_id();
        record.url = "postgresql://new-user:new@db.example:5432/notes".into();
        assert_eq!(record.target_id(), id);
        record.url = "postgres://user:new@db.example/notes-replacement".into();
        assert_ne!(record.target_id(), id);
        record.url = "postgres://user:new@db.example/notes?host=another.example".into();
        assert_ne!(record.target_id(), id);
    }

    #[tokio::test]
    async fn local_query_paginates_read_only_with_cloud_result_shape() {
        let temp = tempfile::tempdir().unwrap();
        let workspace = workspace(temp.path(), "workspace", "sqlite:///notes.db");
        rusqlite::Connection::open(workspace.join("notes.db"))
            .unwrap()
            .execute_batch("CREATE TABLE notes(n INTEGER); INSERT INTO notes VALUES (1),(2),(3);")
            .unwrap();
        let host = local_host(Some(temp.path().join("config.toml")), vec![workspace], &[]).unwrap();
        let target = Target::local(host.resolve("notes", None, None).unwrap());
        let result = target
            .execute(
                &host,
                None,
                "SELECT n FROM notes ORDER BY n",
                1,
                1,
                "request-1",
            )
            .await
            .unwrap();
        assert_eq!(result["rows"], json!([{"n":2}]));
        assert_eq!(result["requestId"], "request-1");
        assert_eq!(result["resourceId"], target.id());
        assert!(
            target
                .execute(&host, None, "DELETE FROM notes", 1, 0, "request-2")
                .await
                .is_err()
        );
        assert_eq!(
            target.columns(&host, None, "notes").await.unwrap()["rows"][0]["column_name"],
            "n"
        );
    }
}
