use super::arg;
use crate::service::{Failure, Host, Result};
use axum::http::{HeaderMap, StatusCode, header};
use quarry_platform::PersonalStore;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashSet},
    path::{Path, PathBuf},
    sync::{Arc, Mutex, RwLock},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

/// The embedding application supplies account-scoped authentication and a fixed cloud endpoint.
#[async_trait::async_trait]
pub trait CloudBackend: Send + Sync {
    /// Verify the current local account before modifying scoped records.
    /// Backends without a cache can fall back to fetching the catalog.
    async fn authorized_resource_ids(&self) -> Result<Option<Vec<String>>> {
        Ok(None)
    }

    async fn request(
        &self,
        method: &str,
        path: &str,
        pairs: &[(&str, String)],
        body: Option<Value>,
    ) -> Result<Value>;
}

#[derive(Clone)]
pub struct NativeHistory {
    store: PersonalStore,
    origin: String,
    owner: String,
    scope: String,
}

impl NativeHistory {
    pub fn new(path: PathBuf, origin: String, owner: String) -> Self {
        let scope = format!(
            "{:x}",
            Sha256::digest(format!("{origin}\n{owner}\n{}", path.display()))
        );
        Self {
            store: PersonalStore::new(path),
            origin,
            owner,
            scope,
        }
    }

    pub fn for_host(host: &Host) -> Self {
        Self::new(
            host.state_dir.join("personal.sqlite3"),
            "local".into(),
            format!(
                "{:x}",
                Sha256::digest(host.config_path.as_os_str().as_encoded_bytes())
            ),
        )
    }

    fn entries(&self) -> Result<Value> {
        self.store
            .get(&self.origin, &self.owner, "query-history", "entries")
    }

    pub async fn workbench(&self, update: Option<Value>) -> Result<Value> {
        let history = self.clone();
        tokio::task::spawn_blocking(move || match update {
            None => history
                .store
                .get(&history.origin, &history.owner, "workbench", "workspace"),
            Some(update) => {
                let revision = update["revision"]
                    .as_u64()
                    .ok_or_else(|| Failure::invalid("Revision required"))?;
                if !update["value"]["values"]
                    .as_object()
                    .is_some_and(|values| values.values().all(Value::is_string))
                {
                    return Err(Failure::invalid("Invalid workbench state"));
                }
                history.store.put(
                    &history.origin,
                    &history.owner,
                    "workbench",
                    "workspace",
                    update["value"].clone(),
                    revision,
                )
            }
        })
        .await
        .map_err(|_| storage_unavailable())?
    }

    pub async fn visible_workbench(&self, cloud_ids: &HashSet<String>) -> Result<Value> {
        let mut document = self.workbench(None).await?;
        if let Some(values) = document
            .get_mut("value")
            .and_then(|value| value.get_mut("values"))
            .and_then(Value::as_object_mut)
        {
            *values = project_records(values, cloud_ids, true);
        }
        Ok(document)
    }

    async fn save_visible_workbench(
        &self,
        mut update: Value,
        cloud_ids: &HashSet<String>,
    ) -> Result<Value> {
        let previous = self.workbench(None).await?;
        let editing_cloud_ids: HashSet<String> = update["cloudIds"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .filter(|id| cloud_ids.contains(*id))
            .map(String::from)
            .collect();
        let values = update["value"]["values"]
            .as_object_mut()
            .ok_or_else(|| Failure::invalid("Invalid workbench state"))?;
        if !values.values().all(Value::is_string) {
            return Err(Failure::invalid("Invalid workbench state"));
        }
        *values = project_records(values, &editing_cloud_ids, true);
        if let Some(previous) = previous["value"]["values"].as_object() {
            let hidden = project_records(previous, &editing_cloud_ids, false);
            merge_hidden_records(values, &hidden);
        }
        let saved = self.workbench(Some(update)).await?;
        Ok(json!({"revision":saved["revision"]}))
    }

    pub async fn read_entries(&self) -> Result<Vec<Value>> {
        let history = self.clone();
        tokio::task::spawn_blocking(move || {
            Ok(history.entries()?["value"]
                .as_array()
                .cloned()
                .unwrap_or_default())
        })
        .await
        .map_err(|_| storage_unavailable())?
    }

    pub async fn append_entry(&self, entry: Value) -> Result<()> {
        let history = self.clone();
        tokio::task::spawn_blocking(move || history.append(entry))
            .await
            .map_err(|_| storage_unavailable())?
    }

    pub(super) async fn record(
        &self,
        target: Value,
        resource_id: Option<&str>,
        sql: &str,
        request_id: Option<&str>,
    ) {
        let entry = json!({
            "id":request_id.filter(|id| !id.is_empty() && id.len() <= 100).map(String::from).unwrap_or_else(new_id),
            "resourceId":resource_id, "sql":sql, "target":target,
            "createdAt":SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64,
        });
        if self.append_entry(entry).await.is_err() {
            eprintln!("Query completed, but local Quarry history could not be saved");
        }
    }

    fn append(&self, entry: Value) -> Result<()> {
        for _ in 0..3 {
            let document = self.entries()?;
            let mut entries = document["value"].as_array().cloned().unwrap_or_default();
            if entries.iter().any(|old| old["id"] == entry["id"]) {
                return Ok(());
            }
            entries.insert(0, entry.clone());
            entries.truncate(1000);
            // Keep older CLI results when they fit, then trim the oldest history.
            let mut result_budget = 1536 * 1024;
            for entry in &mut entries {
                let Some(result) = entry.get_mut("result") else {
                    continue;
                };
                let size = result.to_string().len();
                if size > result_budget {
                    *result = Value::Null;
                } else {
                    result_budget -= size;
                }
            }
            let mut size: usize = entries
                .iter()
                .map(|entry| entry.to_string().len() + 1)
                .sum();
            while size > 3 * 1024 * 1024 && entries.len() > 1 {
                size -= entries.pop().expect("oldest query").to_string().len() + 1;
            }
            match self.store.put(
                &self.origin,
                &self.owner,
                "query-history",
                "entries",
                json!(entries),
                document["revision"].as_u64().unwrap_or_default(),
            ) {
                Ok(_) => return Ok(()),
                Err(error) if error.status == 409 => continue,
                Err(error) => return Err(error),
            }
        }
        Err(storage_unavailable())
    }
}

fn record_array(values: &serde_json::Map<String, Value>, key: &str) -> Vec<Value> {
    values
        .get(key)
        .and_then(Value::as_str)
        .and_then(|value| serde_json::from_str(value).ok())
        .unwrap_or_default()
}

fn record_visible(env: Option<&str>, cloud_ids: &HashSet<String>) -> bool {
    if let Some(id) = cloud_id(env) {
        return cloud_ids.contains(id);
    }
    !env.is_some_and(|env| env.starts_with("remote:"))
}

fn project_records(
    values: &serde_json::Map<String, Value>,
    cloud_ids: &HashSet<String>,
    visible: bool,
) -> serde_json::Map<String, Value> {
    let mut output = if visible {
        values.clone()
    } else {
        serde_json::Map::new()
    };
    let tabs = record_array(values, "qy_tabs");
    let snapshots = record_array(values, "qy_tabres");
    if values.contains_key("qy_tabs") {
        let active = values
            .get("qy_ati")
            .and_then(Value::as_str)
            .and_then(|i| i.parse::<usize>().ok());
        let filtered: Vec<_> = tabs
            .into_iter()
            .enumerate()
            .filter(|(_, tab)| record_visible(tab["env"].as_str(), cloud_ids) == visible)
            .collect();
        let index = active.and_then(|active| filtered.iter().position(|(i, _)| *i == active));
        output.insert(
            "qy_ati".into(),
            json!(index.map_or_else(|| "-1".into(), |i| i.to_string())),
        );
        let filtered_tabs: Vec<_> = filtered.iter().map(|(_, tab)| tab).collect();
        let filtered_results: Vec<_> = filtered
            .iter()
            .map(|(i, _)| {
                snapshots
                    .get(*i)
                    .filter(|result| {
                        cloud_id(result["env"].as_str()).is_none()
                            && record_visible(result["env"].as_str(), cloud_ids) == visible
                    })
                    .unwrap_or(&Value::Null)
            })
            .collect();
        output.insert("qy_tabs".into(), json!(json!(filtered_tabs).to_string()));
        if values.contains_key("qy_tabres") {
            output.insert(
                "qy_tabres".into(),
                json!(json!(filtered_results).to_string()),
            );
        }
    }
    if values.contains_key("qy_hist") {
        let history: Vec<_> = record_array(values, "qy_hist")
            .into_iter()
            .filter(|entry| record_visible(entry["env"].as_str(), cloud_ids) == visible)
            .collect();
        output.insert("qy_hist".into(), json!(json!(history).to_string()));
    }
    for key in ["qy_ui", "qy_result"] {
        if let Some(value) = values
            .get(key)
            .and_then(Value::as_str)
            .and_then(|value| serde_json::from_str::<Value>(value).ok())
            && (!record_visible(value["env"].as_str(), cloud_ids)
                || (key == "qy_result" && cloud_id(value["env"].as_str()).is_some()))
        {
            if visible {
                output.remove(key);
            } else {
                output.insert(key.into(), values[key].clone());
            }
        }
    }
    output
}

fn merge_hidden_records(
    values: &mut serde_json::Map<String, Value>,
    hidden: &serde_json::Map<String, Value>,
) {
    let mut tabs = record_array(values, "qy_tabs");
    let mut results = record_array(values, "qy_tabres");
    results.resize(tabs.len(), Value::Null);
    let old_results = record_array(hidden, "qy_tabres");
    let mut used: HashSet<String> = tabs
        .iter()
        .filter_map(|tab| tab["id"].as_str().map(String::from))
        .collect();
    let mut highest = used
        .iter()
        .filter_map(|id| id.strip_prefix('t')?.parse::<u64>().ok())
        .max()
        .unwrap_or_default();
    for (index, mut tab) in record_array(hidden, "qy_tabs").into_iter().enumerate() {
        if tabs
            .iter()
            .any(|current| current["id"] == tab["id"] && current["env"] == tab["env"])
        {
            continue;
        }
        if tab["id"].as_str().is_some_and(|id| used.contains(id)) {
            loop {
                highest += 1;
                let id = format!("t{highest}");
                if used.insert(id.clone()) {
                    tab["id"] = json!(id);
                    break;
                }
            }
        } else if let Some(id) = tab["id"].as_str() {
            used.insert(id.to_owned());
        }
        tabs.push(tab);
        results.push(old_results.get(index).cloned().unwrap_or(Value::Null));
    }
    if !tabs.is_empty() {
        values.insert("qy_tabs".into(), json!(json!(tabs).to_string()));
        values.insert("qy_tabres".into(), json!(json!(results).to_string()));
    }
    let mut history = record_array(values, "qy_hist");
    for entry in record_array(hidden, "qy_hist") {
        if !history.contains(&entry) {
            history.push(entry);
        }
    }
    if !history.is_empty() {
        values.insert("qy_hist".into(), json!(json!(history).to_string()));
    }
    for key in ["qy_ui", "qy_result"] {
        if let Some(value) = hidden.get(key) {
            values
                .entry(key.to_owned())
                .or_insert_with(|| value.clone());
        }
    }
}

fn storage_unavailable() -> Failure {
    Failure {
        status: 503,
        code: "storage_unavailable".into(),
        message: "Local history is unavailable".into(),
    }
}

#[derive(Clone, Deserialize)]
pub(super) struct CloudResource {
    id: String,
    project: String,
    db: String,
    env: Option<String>,
    engine: quarry_core::Engine,
    production: bool,
    #[serde(default)]
    provider: Option<crate::federation::ProviderIdentity>,
    #[serde(default)]
    association: Option<crate::federation::LocalTarget>,
    #[serde(default)]
    status: Option<crate::federation::Status>,
}

#[derive(Deserialize)]
struct CloudCatalog {
    resources: Vec<CloudResource>,
    status: Option<crate::federation::Status>,
    #[serde(default)]
    providers: Vec<crate::federation::ProviderStatus>,
}

pub(crate) struct NativeWorkbench {
    backend: Arc<dyn CloudBackend>,
    resources: RwLock<Vec<CloudResource>>,
    pub(super) history: NativeHistory,
    launch_code: Mutex<Option<String>>,
    launch_created: Instant,
    session: String,
    require_session: bool,
    registry: Option<crate::federation::Registry>,
    catalog_refresh: tokio::sync::Mutex<()>,
}

impl NativeWorkbench {
    pub(crate) fn new(backend: Arc<dyn CloudBackend>, history: NativeHistory) -> Arc<Self> {
        Self::with_session(backend, history, None, true)
    }

    pub(crate) fn standalone(
        backend: crate::federation::Federation,
        history: NativeHistory,
        require_session: bool,
    ) -> Arc<Self> {
        let registry = backend.registry().clone();
        Self::with_session(Arc::new(backend), history, Some(registry), require_session)
    }

    fn with_session(
        backend: Arc<dyn CloudBackend>,
        history: NativeHistory,
        registry: Option<crate::federation::Registry>,
        require_session: bool,
    ) -> Arc<Self> {
        Arc::new(Self {
            backend,
            history,
            resources: RwLock::default(),
            launch_code: Mutex::new(Some(new_id())),
            launch_created: Instant::now(),
            session: new_id(),
            require_session,
            registry,
            catalog_refresh: tokio::sync::Mutex::new(()),
        })
    }

    pub(crate) fn requires_session(&self) -> bool {
        self.require_session
    }

    pub(super) fn shared_records(&self) -> bool {
        self.require_session
    }

    pub(super) fn is_standalone(&self) -> bool {
        self.registry.is_some()
    }

    pub(crate) fn launch_url(&self, url: &str) -> String {
        let code = self.launch_code.lock().expect("launch code mutex");
        format!("{url}#quarry_workbench={}", code.as_deref().unwrap_or(""))
    }

    pub(super) fn storage_scope(&self) -> &str {
        &self.history.scope
    }

    pub(super) fn establish(&self, code: &str, port: u16) -> Result<String> {
        let mut saved = self.launch_code.lock().map_err(|_| session_required())?;
        if self.launch_created.elapsed() > Duration::from_secs(300)
            || saved.as_deref() != Some(code)
        {
            return Err(session_required());
        }
        *saved = None;
        Ok(format!(
            "quarry_workbench_{port}={}; HttpOnly; SameSite=Strict; Path=/api",
            self.session
        ))
    }

    pub(super) fn authorized(&self, headers: &HeaderMap, port: u16) -> bool {
        let expected = format!("quarry_workbench_{port}={}", self.session);
        headers.get_all(header::COOKIE).iter().any(|value| {
            value
                .to_str()
                .is_ok_and(|value| value.split(';').any(|cookie| cookie.trim() == expected))
        })
    }

    async fn refresh_catalog(&self) -> (Value, bool) {
        let _refresh = self.catalog_refresh.lock().await;
        let outcome = tokio::time::timeout(
            Duration::from_secs(8),
            self.backend.request("GET", "/api/platform", &[], None),
        )
        .await;
        match outcome {
            Ok(Ok(document)) => match serde_json::from_value::<CloudCatalog>(document) {
                Ok(catalog) => {
                    let visible = self.require_session
                        || catalog.status.is_none()
                        || !catalog.providers.is_empty();
                    let status = catalog
                        .status
                        .map_or_else(|| json!("connected"), |status| json!(status));
                    *self.resources.write().expect("cloud catalog lock") = catalog.resources;
                    (status, visible)
                }
                Err(_) => {
                    self.resources.write().expect("cloud catalog lock").clear();
                    (json!("unavailable"), true)
                }
            },
            Ok(Err(error)) if matches!(error.status, 401 | 403) => {
                self.resources.write().expect("cloud catalog lock").clear();
                if error.status == 401 {
                    (json!("signed_out"), true)
                } else {
                    (json!("forbidden"), true)
                }
            }
            _ => {
                self.resources.write().expect("cloud catalog lock").clear();
                (json!("unavailable"), true)
            }
        }
    }

    pub(super) async fn connections(&self, mut local: Value) -> Value {
        if let Some(groups) = local["groups"].as_array_mut() {
            distinguish_local_targets(groups);
        }
        if !self.require_session {
            if self
                .registry
                .as_ref()
                .is_some_and(|registry| registry.has_providers().unwrap_or(false))
            {
                local["cloudStatus"] = json!("restart_required");
            }
            return local;
        }
        let (status, visible_status) = self.refresh_catalog().await;
        if visible_status {
            local["cloudStatus"] = status;
        }
        if let Some(groups) = local["groups"].as_array_mut() {
            for resource in self.resources.read().expect("cloud catalog lock").iter() {
                resource.add_to(groups);
            }
        }
        local
    }

    fn visible_cloud_ids(&self) -> HashSet<String> {
        self.resources
            .read()
            .expect("cloud catalog lock")
            .iter()
            .map(|resource| resource.id.clone())
            .collect()
    }

    pub(super) async fn workbench(&self, update: Option<Value>) -> Result<Value> {
        if let Some(update) = update {
            let cloud_ids = if update["cloudIds"]
                .as_array()
                .is_some_and(|ids| !ids.is_empty())
            {
                match self.backend.authorized_resource_ids().await {
                    Ok(Some(ids)) => ids.into_iter().collect(),
                    Ok(None) => {
                        self.refresh_catalog().await;
                        self.visible_cloud_ids()
                    }
                    Err(_) => HashSet::new(),
                }
            } else {
                HashSet::new()
            };
            self.history
                .save_visible_workbench(update, &cloud_ids)
                .await
        } else {
            self.refresh_catalog().await;
            let ids = self.visible_cloud_ids();
            let mut document = self.history.visible_workbench(&ids).await?;
            document["cloudIds"] = json!(ids);
            Ok(document)
        }
    }

    pub(super) async fn read_history(&self) -> Result<Value> {
        self.refresh_catalog().await;
        let entries = self.history.read_entries().await?;
        let entries: Vec<_> = entries
            .iter()
            .filter_map(|entry| {
                let sql = entry["sql"].as_str()?;
                let target = &entry["target"];
                let env = if target["placement"] == "remote" {
                    let id = entry["resourceId"].as_str()?;
                    self.resource(id).ok()?;
                    Some(route_key(id))
                } else {
                    target["env"].as_str().map(String::from)
                };
                Some(json!({"sql":sql,"db":target["db"],"env":env,"targetId":entry["resourceId"],"ts":entry["createdAt"]}))
            })
            .collect();
        Ok(json!({"history":entries}))
    }

    pub(super) fn resource(&self, id: &str) -> Result<CloudResource> {
        self.resources
            .read()
            .expect("cloud catalog lock")
            .iter()
            .find(|r| r.id == id)
            .cloned()
            .ok_or_else(|| Failure {
                status: 404,
                code: "not_found".into(),
                message: "Cloud database is unavailable. Refresh the connection list.".into(),
            })
    }

    pub(super) async fn get(
        &self,
        path: &str,
        id: &str,
        q: &BTreeMap<String, String>,
    ) -> Result<Value> {
        if path == "/api/conninfo" {
            self.refresh_catalog().await;
            let resource = self.resource(id)?;
            return Ok(
                json!({"key":resource.id,"db":resource.db,"env":resource.env,"engine":resource.engine,"production":resource.production,
                "group":resource.project,"source":"cloud","url":null,"host":null,"port":null,"database":null,"region":null,"notes":null,"file":null,"tunnel":null}),
            );
        }
        self.resource(id)?;
        if path == "/api/health" && arg(q, "cached") == Some("1") {
            return Ok(json!({"ok":null}));
        }
        let mut pairs = vec![("resourceId", id.to_owned())];
        pairs.extend(
            ["table", "key"]
                .into_iter()
                .filter_map(|key| arg(q, key).map(|value| (key, value.to_owned()))),
        );
        let outcome = self.backend.request("GET", path, &pairs, None).await;
        match outcome {
            Err(error) if path == "/api/health" => Ok(json!({"ok":false,"error":error.message})),
            other => other,
        }
    }

    pub(super) async fn post(&self, path: &str, id: &str, mut body: Value) -> Result<Value> {
        self.resource(id)?;
        if body["target"].as_str().is_some_and(|target| target != id) {
            return Err(Failure::invalid(
                "Cloud target changed. Refresh the connection list.",
            ));
        }
        let fields = body.as_object_mut().expect("JSON request object");
        fields.remove("db");
        fields.remove("env");
        fields.remove("workspace");
        fields.remove("target");
        fields.insert("resourceId".into(), json!(id));
        if path == "/api/query" && body["maxRows"] == 0 {
            body["maxRows"] = json!(5000);
        }
        self.backend.request("POST", path, &[], Some(body)).await
    }
}

pub(super) fn session_required() -> Failure {
    Failure {
        status: StatusCode::UNAUTHORIZED.as_u16(),
        code: "workbench_session_required".into(),
        message: "Open this local workbench from ylab db gui.".into(),
    }
}

fn new_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

fn route_key(id: &str) -> String {
    format!("cloud:{id}")
}

pub(super) fn cloud_id(env: Option<&str>) -> Option<&str> {
    env.and_then(|env| env.strip_prefix("cloud:"))
}

pub(super) const LOCAL_ROUTE_PREFIX: &str = "local-target:";
pub(super) const LOCAL_DB_PREFIX: &str = "local-db";

fn distinguish_local_targets(groups: &mut [Value]) {
    let mut dbs = BTreeMap::<String, usize>::new();
    let mut names = BTreeMap::<(String, Option<String>), usize>::new();
    for item in groups
        .iter()
        .flat_map(|group| group["items"].as_array().into_iter().flatten())
    {
        let Some(db) = item["db"].as_str() else {
            continue;
        };
        *dbs.entry(db.into()).or_default() += 1;
        for env in item["envs"].as_array().into_iter().flatten() {
            *names
                .entry((db.into(), env["env"].as_str().map(String::from)))
                .or_default() += 1;
        }
    }
    for group in groups {
        let group_id = group["group"].clone();
        for item in group["items"].as_array_mut().into_iter().flatten() {
            let Some(db) = item["db"].as_str().map(String::from) else {
                continue;
            };
            let alias = dbs.get(&db).is_some_and(|count| *count > 1);
            if alias {
                item["db"] = json!(opaque_key(LOCAL_DB_PREFIX, &(&group_id, &db)));
                item["label"] = json!(db);
            }
            for env in item["envs"].as_array_mut().into_iter().flatten() {
                let key = (db.clone(), env["env"].as_str().map(String::from));
                if (alias || names.get(&key).is_some_and(|count| *count > 1))
                    && let (Some(target), Some(workspace)) =
                        (env["targetId"].as_str(), env["workspace"].as_str())
                {
                    let route = format!("{LOCAL_ROUTE_PREFIX}{target}");
                    let label = quarry_service::display_path(Path::new(workspace));
                    env["display_env"] = env["env"].clone();
                    env["env"] = json!(route);
                    env["workspaceLabel"] = json!(label);
                }
            }
        }
    }
}

impl CloudResource {
    pub(super) fn saved_sql(
        &self,
        host: &Host,
        db: &str,
        workspace: &Path,
        sql: &str,
        parameters: &BTreeMap<String, String>,
    ) -> Result<(String, String)> {
        let mismatch = || Failure::invalid("Saved query belongs to a different database");
        let logical_db = if self.provider.is_some() {
            let association = self.association.as_ref().ok_or_else(mismatch)?;
            let record = crate::federated_cli::resolve_local_target(host, &association.id)?;
            if record.workspace != workspace || (record.logical_db() != db && record.key != db) {
                return Err(mismatch());
            }
            record.logical_db().to_owned()
        } else if self.db == db {
            db.to_owned()
        } else {
            return Err(mismatch());
        };
        Ok((
            logical_db,
            quarry_core::policy::substitute(self.engine, sql, parameters)?,
        ))
    }

    fn add_to(&self, groups: &mut Vec<Value>) {
        if let Some(provider) = &self.provider {
            self.add_federated(groups, provider);
            return;
        }
        let matching_group = groups.iter().position(|group| {
            group["group"]
                .as_str()
                .is_some_and(|name| name.eq_ignore_ascii_case(&self.project))
        });
        // Legacy tabs address databases by name. Disambiguate a new name
        // collision across projects before it reaches the tab store.
        let db = if groups.iter().enumerate().any(|(index, group)| {
            Some(index) != matching_group
                && group["items"]
                    .as_array()
                    .is_some_and(|items| items.iter().any(|item| item["db"] == self.db))
        }) {
            format!("{}/{}", self.project, self.db)
        } else {
            self.db.clone()
        };
        let group = if let Some(index) = matching_group {
            &mut groups[index]
        } else {
            groups.push(json!({"group":self.project,"ws":null,"items":[]}));
            groups.last_mut().expect("added cloud group")
        };
        let items = group["items"].as_array_mut().expect("connection items");
        let item = if let Some(index) = items.iter().position(|item| item["db"] == db) {
            &mut items[index]
        } else {
            items.push(json!({"db":db,"is_env_set":true,"engine":self.engine,"envs":[]}));
            items.last_mut().expect("added cloud database")
        };
        item["is_env_set"] = json!(true);
        let env = route_key(&self.id);
        let envs = item["envs"]
            .as_array_mut()
            .expect("connection environments");
        if !envs.iter().any(|entry| entry["env"] == env) {
            envs.push(json!({"env":env,"display_env":self.env,"source":"cloud","key":self.id,"resourceId":self.id,
                "engine":self.engine,"production":self.production,"region":null,"ssh":false,"proxied":false}));
        }
    }

    fn add_federated(
        &self,
        groups: &mut Vec<Value>,
        provider: &crate::federation::ProviderIdentity,
    ) {
        let entry = json!({"env":route_key(&self.id),"display_env":self.env,"source":"cloud","key":self.id,"resourceId":self.id,
            "targetId":self.id,"providerLabel":format!("YLab · {} · {}",provider.origin,provider.profile),
            "sourceStatus":self.status,"engine":self.engine,"production":self.production,"region":null,"ssh":false,"proxied":false});
        if let Some(association) = &self.association {
            for group in groups.iter_mut() {
                for item in group["items"].as_array_mut().into_iter().flatten() {
                    let Some(envs) = item["envs"].as_array_mut() else {
                        continue;
                    };
                    if envs.iter().any(|env| env["targetId"] == association.id) {
                        envs.push(entry);
                        item["is_env_set"] = json!(true);
                        return;
                    }
                }
            }
        }
        // Project and database display names can collide across providers or with local names.
        let provider_label = format!("YLab · {} · {}", provider.origin, provider.profile);
        let group_id = opaque_key("cloud-group", &(provider.scope_id(), &self.project));
        let db_id = opaque_key("cloud-db", &(&group_id, &self.db));
        let group = if let Some(index) = groups.iter().position(|group| group["group"] == group_id)
        {
            &mut groups[index]
        } else {
            groups.push(json!({"group":group_id,"label":format!("{} · {}",self.project,provider_label),"ws":null,"items":[]}));
            groups.last_mut().expect("added cloud group")
        };
        let items = group["items"].as_array_mut().expect("connection items");
        let item = if let Some(index) = items.iter().position(|item| item["db"] == db_id) {
            &mut items[index]
        } else {
            items.push(json!({"db":db_id,"label":self.db,"is_env_set":true,"engine":self.engine,"envs":[]}));
            items.last_mut().expect("added cloud database")
        };
        item["envs"]
            .as_array_mut()
            .expect("connection environments")
            .push(entry);
    }
}

fn opaque_key(prefix: &str, parts: &impl serde::Serialize) -> String {
    format!(
        "{prefix}:{:x}",
        Sha256::digest(serde_json::to_vec(parts).expect("serializable identity"))
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_preserves_local_env_and_distinguishes_same_name_cloud_targets() {
        let mut groups = vec![json!({"group":"demo","ws":"/workspace","items":[
            {"db":"notes","engine":"sqlite","envs":[{"env":"dev","key":"local-dev"}]}
        ]})];
        let resource = CloudResource {
            id: "notes-remote".into(),
            project: "demo".into(),
            db: "notes".into(),
            env: Some("dev".into()),
            engine: quarry_core::Engine::Sqlite,
            production: true,
            provider: None,
            association: None,
            status: None,
        };
        resource.add_to(&mut groups);
        assert_eq!(groups[0]["items"][0]["envs"][0]["env"], "dev");
        assert_eq!(
            groups[0]["items"][0]["envs"][1]["env"],
            "cloud:notes-remote"
        );
        assert_eq!(groups[0]["items"][0]["envs"][1]["display_env"], "dev");
        assert_eq!(groups[0]["items"][0]["envs"][1]["production"], true);

        CloudResource {
            project: "other".into(),
            id: "other-notes".into(),
            ..resource
        }
        .add_to(&mut groups);
        assert_eq!(groups[1]["items"][0]["db"], "other/notes");
        assert_eq!(groups[1]["items"][0]["envs"][0]["env"], "cloud:other-notes");
    }

    #[test]
    fn federation_only_merges_explicit_endpoint_bound_associations() {
        let provider =
            crate::federation::ProviderIdentity::ylab("https://ylab.example/", "default", "alice")
                .unwrap();
        let mut groups = vec![json!({"group":"demo","ws":"/workspace","items":[
            {"db":"notes","engine":"sqlite","envs":[{"env":"dev","key":"dev","targetId":"local:original"}]}
        ]})];
        let mut resource = CloudResource {
            id: provider.route_id("notes"),
            project: "demo".into(),
            db: "notes".into(),
            env: Some("dev".into()),
            engine: quarry_core::Engine::Sqlite,
            production: false,
            provider: Some(provider),
            association: None,
            status: Some(crate::federation::Status::Connected),
        };
        resource.add_to(&mut groups);
        assert_eq!(groups.len(), 2);
        assert_eq!(groups[1]["items"][0]["label"], "notes");
        assert_eq!(groups[0]["items"][0]["envs"].as_array().unwrap().len(), 1);
        groups.pop();
        resource.association = Some(crate::federation::LocalTarget {
            workspace: "/workspace".into(),
            key: "dev".into(),
            id: "local:original".into(),
        });
        resource.add_to(&mut groups);
        assert_eq!(groups.len(), 1);
        assert_eq!(
            groups[0]["items"][0]["envs"][1]["env"],
            route_key(&resource.id)
        );
        let envs = groups[0]["items"][0]["envs"].as_array_mut().unwrap();
        envs.pop();
        envs[0]["targetId"] = json!("local:replacement");
        resource.add_to(&mut groups);
        assert_eq!(groups.len(), 2);
    }

    #[tokio::test]
    async fn workbench_projection_keeps_other_accounts_drafts_private_on_update() {
        let dir = tempfile::tempdir().unwrap();
        let history = NativeHistory::new(
            dir.path().join("personal.sqlite3"),
            "local".into(),
            "local".into(),
        );
        let tabs = json!([
            {"id":"t1","env":"cloud:f:alice","sql":"alice draft"},
            {"id":"t2","env":"dev","sql":"local draft"},
            {"id":"t3","env":"cloud:f:bob","sql":"bob draft"}
        ]);
        let results = json!([
            {"env":"cloud:f:alice","res":{"rows":["alice rows"]}},
            null,
            {"env":"cloud:f:bob","res":{"rows":["bob rows"]}}
        ]);
        let entries = json!([{"env":"cloud:f:alice","sql":"alice history"},{"env":"dev","sql":"local history"}]);
        history.workbench(Some(json!({"revision":0,"value":{"values":{
            "qy_tabs":tabs.to_string(),"qy_tabres":results.to_string(),"qy_hist":entries.to_string(),"qy_ati":"2"
        }}}))).await.unwrap();
        let bob = HashSet::from(["f:bob".to_owned()]);
        let mut view = history.visible_workbench(&bob).await.unwrap();
        assert!(!view.to_string().contains("alice"));
        assert!(view.to_string().contains("local draft"));
        assert!(!view.to_string().contains("bob rows"));
        assert_eq!(view["value"]["values"]["qy_ati"], "1");
        view["cloudIds"] = json!(["f:bob"]);
        view["value"]["values"]["qy_tabs"] =
            json!(json!([{"id":"t2","env":"dev","sql":"local edit"},{"id":"t1","env":"cloud:f:alice","sql":"forged overwrite"}]).to_string());
        history.save_visible_workbench(view, &bob).await.unwrap();
        let alice = history
            .visible_workbench(&HashSet::from(["f:alice".to_owned()]))
            .await
            .unwrap()
            .to_string();
        assert!(
            alice.contains("alice draft")
                && !alice.contains("alice rows")
                && alice.contains("alice history")
        );
        assert!(alice.contains("local edit"));
        assert!(!alice.contains("forged overwrite"));
        assert!(!alice.contains("bob"));
    }

    #[tokio::test]
    async fn native_workbench_adopts_existing_history_and_drafts() {
        let dir = tempfile::tempdir().unwrap();
        let origin = "https://ylab.example";
        let file = dir.path().join("state.sqlite3");
        let store = PersonalStore::new(file.clone());
        store
            .put(
                origin,
                "alice",
                "workbench",
                "workspace",
                json!({"values":{"qy_hist":"[]"}}),
                0,
            )
            .unwrap();
        store
            .put(
                origin,
                "alice",
                "query-history",
                "entries",
                json!([{
                    "id":"prior", "resourceId":"notes-dev", "sql":"SELECT 73", "createdAt":5,
                    "target":{"db":"notes","env":"dev","placement":"remote"}
                }]),
                0,
            )
            .unwrap();

        let history = NativeHistory::new(file.clone(), origin.into(), "alice".into());
        let personal = history.workbench(None).await.unwrap();
        assert_eq!(personal["value"]["values"]["qy_hist"], "[]");
        let conflict = history
            .workbench(Some(json!({"value":{"values":{}},"revision":0})))
            .await
            .unwrap_err();
        assert_eq!(conflict.status, 409);

        history
            .record(
                json!({"db":"notes","env":"local","placement":"local"}),
                None,
                "SELECT 42",
                Some("query"),
            )
            .await;
        history
            .record(
                json!({"db":"notes","env":"local","placement":"local"}),
                None,
                "SELECT 42",
                Some("query"),
            )
            .await;
        let entries = history.read_entries().await.unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0]["target"]["env"], "local");
        assert_eq!(entries[1]["resourceId"], "notes-dev");

        let other = NativeHistory::new(file, origin.into(), "bob".into());
        assert!(other.read_entries().await.unwrap().is_empty());
        assert_eq!(other.workbench(None).await.unwrap()["value"], Value::Null);
    }
}
