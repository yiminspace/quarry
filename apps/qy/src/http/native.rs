use super::arg;
use crate::service::{Failure, Result};
use axum::http::{HeaderMap, StatusCode, header};
use quarry_platform::PersonalStore;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{Arc, Mutex, RwLock},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

/// The embedding application supplies account-scoped authentication and a fixed cloud endpoint.
#[async_trait::async_trait]
pub trait CloudBackend: Send + Sync {
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

    fn entries(&self) -> Result<Value> {
        self.store
            .get(&self.origin, &self.owner, "query-history", "entries")
    }

    pub(super) async fn workbench(&self, update: Option<Value>) -> Result<Value> {
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

    pub(super) async fn read(&self) -> Result<Value> {
        let history = self.clone();
        tokio::task::spawn_blocking(move || {
            let document = history.entries()?;
            let entries: Vec<_> = document["value"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|entry| {
                    let sql = entry["sql"].as_str()?;
                    let target = &entry["target"];
                    let env = if target["placement"] == "remote" {
                        entry["resourceId"].as_str().map(route_key)
                    } else {
                        target["env"].as_str().map(String::from)
                    };
                    Some(json!({"sql":sql,"db":target["db"],"env":env,"ts":entry["createdAt"]}))
                })
                .collect();
            Ok(json!({"history":entries}))
        })
        .await
        .map_err(|_| storage_unavailable())?
    }

    pub(super) async fn record(
        &self,
        db: &str,
        env: Option<&str>,
        sql: &str,
        request_id: Option<&str>,
    ) {
        let cloud = cloud_id(env);
        let entry = json!({
            "id":request_id.filter(|id| !id.is_empty() && id.len() <= 100).map(String::from).unwrap_or_else(new_id),
            "resourceId":cloud, "sql":sql,
            "target":{"db":db,"env":env,"placement":if cloud.is_some() {"remote"} else {"local"}},
            "createdAt":SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64,
        });
        let history = self.clone();
        let saved = tokio::task::spawn_blocking(move || history.append(entry)).await;
        if !matches!(saved, Ok(Ok(()))) {
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
}

#[derive(Deserialize)]
struct CloudCatalog {
    resources: Vec<CloudResource>,
}

pub(crate) struct NativeWorkbench {
    backend: Arc<dyn CloudBackend>,
    resources: RwLock<Vec<CloudResource>>,
    pub(super) history: NativeHistory,
    launch_code: Mutex<Option<String>>,
    launch_created: Instant,
    session: String,
}

impl NativeWorkbench {
    pub(crate) fn new(backend: Arc<dyn CloudBackend>, history: NativeHistory) -> Arc<Self> {
        Arc::new(Self {
            backend,
            history,
            resources: RwLock::default(),
            launch_code: Mutex::new(Some(new_id())),
            launch_created: Instant::now(),
            session: new_id(),
        })
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

    pub(super) async fn connections(&self, mut local: Value) -> Value {
        let outcome = tokio::time::timeout(
            Duration::from_secs(3),
            self.backend.request("GET", "/api/platform", &[], None),
        )
        .await;
        let status = match outcome {
            Ok(Ok(document)) => match serde_json::from_value::<CloudCatalog>(document) {
                Ok(catalog) => {
                    *self.resources.write().expect("cloud catalog lock") = catalog.resources;
                    "connected"
                }
                Err(_) => "unavailable",
            },
            Ok(Err(error)) if matches!(error.status, 401 | 403) => {
                self.resources.write().expect("cloud catalog lock").clear();
                if error.status == 401 {
                    "signed_out"
                } else {
                    "forbidden"
                }
            }
            _ => "unavailable",
        };
        local["cloudStatus"] = json!(status);
        if let Some(groups) = local["groups"].as_array_mut() {
            for resource in self.resources.read().expect("cloud catalog lock").iter() {
                resource.add_to(groups);
            }
        }
        local
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
            let resource = self.resource(id)?;
            return Ok(
                json!({"key":resource.id,"db":resource.db,"env":resource.env,"engine":resource.engine,"production":resource.production,
                "group":resource.project,"source":"cloud","url":null,"host":null,"port":null,"database":null,"region":null,"notes":null,"file":null,"tunnel":null}),
            );
        }
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
        let fields = body.as_object_mut().expect("JSON request object");
        fields.remove("db");
        fields.remove("env");
        fields.remove("workspace");
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
        message: "Open this local workbench from ylab db connect.".into(),
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

impl CloudResource {
    pub(super) fn saved_sql(
        &self,
        db: &str,
        sql: &str,
        parameters: &BTreeMap<String, String>,
    ) -> Result<String> {
        if self.db != db {
            return Err(Failure::invalid(
                "Saved query belongs to a different database",
            ));
        }
        Ok(quarry_core::policy::substitute(
            self.engine,
            sql,
            parameters,
        )?)
    }

    fn add_to(&self, groups: &mut Vec<Value>) {
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
            .record("notes", Some("local"), "SELECT 42", Some("query"))
            .await;
        history
            .record("notes", Some("local"), "SELECT 42", Some("query"))
            .await;
        let entries = history.read().await.unwrap();
        assert_eq!(entries["history"].as_array().unwrap().len(), 2);
        assert_eq!(entries["history"][0]["env"], "local");
        assert_eq!(entries["history"][1]["env"], "cloud:notes-dev");

        let other = NativeHistory::new(file, origin.into(), "bob".into());
        assert_eq!(other.read().await.unwrap()["history"], json!([]));
        assert_eq!(other.workbench(None).await.unwrap()["value"], Value::Null);
    }
}
