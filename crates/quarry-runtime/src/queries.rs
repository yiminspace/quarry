use crate::{
    ConnectionRecord, Result, RuntimeError, WorkspaceCatalog, absolute, atomic_write, lock_file,
    validate_name,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryParameter {
    pub name: String,
    #[serde(default = "text_type")]
    pub r#type: String,
    #[serde(default)]
    pub required: bool,
    #[serde(default)]
    pub default: Option<String>,
}
fn text_type() -> String {
    "text".into()
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SavedQuery {
    pub name: String,
    pub db: String,
    #[serde(default)]
    pub desc: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub params: Vec<QueryParameter>,
    #[serde(default)]
    pub schema_sources: Vec<String>,
    #[serde(default)]
    pub source_fingerprint: Option<String>,
    #[serde(default)]
    pub saved_at: Option<String>,
    #[serde(default)]
    pub last_validated: Option<String>,
    pub sql: String,
    #[serde(default)]
    pub path: PathBuf,
    #[serde(default)]
    pub workspace: PathBuf,
}
impl SavedQuery {
    pub fn parse(path: &Path, workspace: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)?;
        let mut meta: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let mut body = vec![];
        let mut header = true;
        for line in text.lines() {
            let stripped = line.trim();
            if header && (stripped.is_empty() || stripped.starts_with("--")) {
                if let Some(v) = stripped
                    .strip_prefix("--")
                    .map(str::trim)
                    .and_then(|s| s.strip_prefix('@'))
                    && let Some((key, value)) = v.split_once(':')
                {
                    meta.entry(key.trim().into())
                        .or_default()
                        .push(value.trim().into());
                    continue;
                }
                if !stripped.is_empty() {
                    body.push(line);
                }
                continue;
            }
            header = false;
            body.push(line);
        }
        let first = |key: &str| meta.get(key).and_then(|v| v.first()).cloned();
        let name = first("name")
            .ok_or_else(|| RuntimeError::Invalid("saved query missing @name".into()))?;
        let db =
            first("db").ok_or_else(|| RuntimeError::Invalid("saved query missing @db".into()))?;
        validate_name(&name)?;
        validate_name(&db)?;
        if path.file_stem().and_then(|v| v.to_str()) != Some(name.as_str()) {
            return Err(RuntimeError::Invalid(
                "query name does not match filename".into(),
            ));
        }
        let mut params = vec![];
        let pattern=regex::Regex::new(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\s*\(\s*([A-Za-z0-9_]+)?\s*(?:,\s*(required|default\s*=\s*[^)]*))?\s*\))?$").unwrap();
        let mut names = BTreeSet::new();
        for raw in meta.get("param").into_iter().flatten() {
            let captures = pattern.captures(raw).ok_or_else(|| {
                RuntimeError::Invalid("invalid query parameter definition".into())
            })?;
            let name = captures[1].to_string();
            if !names.insert(name.clone()) {
                return Err(RuntimeError::Invalid("duplicate query parameter".into()));
            }
            let spec = captures.get(3).map(|v| v.as_str()).unwrap_or("");
            params.push(QueryParameter {
                name,
                r#type: captures
                    .get(2)
                    .map(|v| v.as_str().into())
                    .unwrap_or_else(text_type),
                required: spec == "required",
                default: spec
                    .strip_prefix("default")
                    .and_then(|v| v.trim_start().strip_prefix('='))
                    .map(|v| v.trim().into()),
            });
        }
        Ok(Self {
            name,
            db,
            desc: meta.get("desc").map(|v| v.join(" ")).unwrap_or_default(),
            tags: meta
                .get("tags")
                .into_iter()
                .flatten()
                .flat_map(|v| v.split(','))
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(String::from)
                .collect(),
            params,
            schema_sources: meta.get("schema-source").cloned().unwrap_or_default(),
            source_fingerprint: first("source-fingerprint"),
            saved_at: first("saved-at"),
            last_validated: first("last-validated"),
            sql: body.join("\n").trim().into(),
            path: path.into(),
            workspace: workspace.into(),
        })
    }
    pub fn parameters(
        &self,
        provided: &BTreeMap<String, String>,
    ) -> Result<BTreeMap<String, String>> {
        let mut out = provided.clone();
        for parameter in &self.params {
            if !out.contains_key(&parameter.name) {
                if let Some(value) = &parameter.default {
                    out.insert(parameter.name.clone(), value.clone());
                } else if parameter.required {
                    return Err(RuntimeError::Invalid(format!(
                        "missing required parameter {}",
                        parameter.name
                    )));
                }
            }
        }
        for (name, value) in &out {
            if let Some(parameter) = self.params.iter().find(|p| &p.name == name) {
                match parameter.r#type.as_str() {
                    "int" | "integer" => {
                        value.parse::<i64>().map_err(|_| {
                            RuntimeError::Invalid(format!("parameter {name} must be an integer"))
                        })?;
                    }
                    "float" | "number" | "decimal" => {
                        if !value.parse::<f64>().is_ok_and(f64::is_finite) {
                            return Err(RuntimeError::Invalid(format!(
                                "parameter {name} must be finite numeric text"
                            )));
                        }
                    }
                    "bool" | "boolean" => {
                        if !["true", "false", "0", "1"].contains(&value.as_str()) {
                            return Err(RuntimeError::Invalid(format!(
                                "parameter {name} must be boolean"
                            )));
                        }
                    }
                    _ => {}
                }
            }
        }
        Ok(out)
    }
    fn render(&self) -> Result<String> {
        validate_name(&self.name)?;
        validate_name(&self.db)?;
        let single = |s: &str| s.replace(['\r', '\n'], " ");
        let mut out = format!("-- @name: {}\n-- @db: {}\n", self.name, self.db);
        if !self.desc.is_empty() {
            out += &format!("-- @desc: {}\n", single(&self.desc));
        }
        if !self.tags.is_empty() {
            out += &format!("-- @tags: {}\n", single(&self.tags.join(", ")));
        }
        let parameter_name = regex::Regex::new(r"^[A-Za-z_][A-Za-z0-9_]*$").unwrap();
        for p in &self.params {
            if !parameter_name.is_match(&p.name) {
                return Err(RuntimeError::Invalid("invalid parameter name".into()));
            }
            let spec = if p.required {
                ", required".into()
            } else {
                p.default
                    .as_ref()
                    .map(|v| format!(", default={}", single(v)))
                    .unwrap_or_default()
            };
            out += &format!("-- @param: {} ({}{})\n", p.name, single(&p.r#type), spec);
        }
        for source in &self.schema_sources {
            out += &format!("-- @schema-source: {}\n", single(source));
        }
        for (k, v) in [
            ("source-fingerprint", &self.source_fingerprint),
            ("saved-at", &self.saved_at),
            ("last-validated", &self.last_validated),
        ] {
            if let Some(v) = v {
                out += &format!("-- @{k}: {}\n", single(v));
            }
        }
        out += &format!("\n{}\n", self.sql.trim());
        Ok(out)
    }
}
#[derive(Debug, Clone)]
pub struct QueryStore {
    pub catalog: WorkspaceCatalog,
}
pub const DEFAULT_QUERY_ISSUE_LIMIT: usize = 100;
pub const MAX_QUERY_ISSUE_LIMIT: usize = 1000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QueryIssueCode {
    InvalidUtf8,
    Parse,
    Read,
    ReadDirectory,
}
/// Safe diagnostics never include SQL, metadata values, credentials or raw OS
/// error strings. Hosts decide whether and where to log these diagnostics.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryIssue {
    pub path: PathBuf,
    pub workspace: PathBuf,
    pub code: QueryIssueCode,
    pub reason: String,
}
impl QueryIssue {
    fn parsing(path: &Path, workspace: &Path, error: &RuntimeError) -> Self {
        let (code, reason) = match error {
            RuntimeError::Io(error) if error.kind() == std::io::ErrorKind::InvalidData => (
                QueryIssueCode::InvalidUtf8,
                "saved query is not valid UTF-8",
            ),
            RuntimeError::Io(error) if error.kind() == std::io::ErrorKind::PermissionDenied => (
                QueryIssueCode::Read,
                "saved query file cannot be read: permission denied",
            ),
            RuntimeError::Io(_) => (QueryIssueCode::Read, "saved query file cannot be read"),
            RuntimeError::Invalid(reason) => {
                let safe = match reason.as_str() {
                    "saved query missing @name" => "saved query missing @name",
                    "saved query missing @db" => "saved query missing @db",
                    "query name does not match filename" => "query name does not match filename",
                    "invalid query parameter definition" => "invalid query parameter definition",
                    "duplicate query parameter" => "duplicate query parameter",
                    "resource name must contain only letters, digits, underscore or hyphen" => {
                        "saved query has an invalid resource name"
                    }
                    _ => "saved query metadata is invalid",
                };
                (QueryIssueCode::Parse, safe)
            }
            _ => (QueryIssueCode::Parse, "saved query cannot be parsed"),
        };
        Self {
            path: path.into(),
            workspace: workspace.into(),
            code,
            reason: reason.into(),
        }
    }
}
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct QueryInventory {
    pub queries: Vec<SavedQuery>,
    pub issues: Vec<QueryIssue>,
    pub omitted_issues: usize,
}
impl QueryInventory {
    fn issue(&mut self, issue: QueryIssue, limit: usize) {
        if self.issues.len() < limit {
            self.issues.push(issue);
        } else {
            self.omitted_issues = self.omitted_issues.saturating_add(1);
        }
    }
}
impl QueryStore {
    pub fn new(catalog: WorkspaceCatalog) -> Self {
        Self { catalog }
    }
    pub fn list(&self) -> Result<Vec<SavedQuery>> {
        Ok(self.scan()?.queries)
    }
    pub fn scan(&self) -> Result<QueryInventory> {
        self.scan_with_issue_limit(DEFAULT_QUERY_ISSUE_LIMIT)
    }
    pub fn scan_with_issue_limit(&self, issue_limit: usize) -> Result<QueryInventory> {
        let limit = issue_limit.min(MAX_QUERY_ISSUE_LIMIT);
        let mut inventory = QueryInventory::default();
        for ws in &self.catalog.workspaces {
            for path in sql_files(&ws.queries_dir, &ws.home, &mut inventory, limit) {
                match SavedQuery::parse(&path, &ws.home) {
                    Ok(query) => inventory.queries.push(query),
                    Err(error) => {
                        inventory.issue(QueryIssue::parsing(&path, &ws.home, &error), limit)
                    }
                }
            }
        }
        inventory
            .queries
            .sort_by(|a, b| a.name.cmp(&b.name).then(a.workspace.cmp(&b.workspace)));
        inventory
            .issues
            .sort_by(|a, b| a.workspace.cmp(&b.workspace).then(a.path.cmp(&b.path)));
        Ok(inventory)
    }
    pub fn describe(&self, name: &str, workspace: Option<&Path>) -> Result<SavedQuery> {
        validate_name(name)?;
        let scope = workspace
            .map(|w| absolute(w, &std::env::current_dir()?))
            .transpose()?;
        let matches = self
            .list()?
            .into_iter()
            .filter(|q| q.name == name && scope.as_ref().is_none_or(|w| &q.workspace == w))
            .collect::<Vec<_>>();
        match matches.len() {
            0 => {
                let mut paths = vec![];
                let mut ignored = QueryInventory::default();
                for ws in self
                    .catalog
                    .workspaces
                    .iter()
                    .filter(|ws| scope.as_ref().is_none_or(|path| path == &ws.home))
                {
                    paths.extend(
                        sql_files(&ws.queries_dir, &ws.home, &mut ignored, 0)
                            .into_iter()
                            .filter(|path| {
                                path.file_stem().and_then(|stem| stem.to_str()) == Some(name)
                            })
                            .map(|path| (path, ws.home.clone())),
                    );
                }
                match paths.as_slice() {
                    [] => Err(RuntimeError::NotFound(name.into())),
                    [(path, workspace)] => SavedQuery::parse(path, workspace).map_err(|error| {
                        let issue = QueryIssue::parsing(path, workspace, &error);
                        RuntimeError::Invalid(format!(
                            "cannot load saved query {name}: {}",
                            issue.reason
                        ))
                    }),
                    _ => Err(RuntimeError::Ambiguous(format!(
                        "query file {name}; select workspace or repair duplicate files"
                    ))),
                }
            }
            1 => Ok(matches.into_iter().next().unwrap()),
            _ => Err(RuntimeError::Ambiguous(format!(
                "query {name}; select workspace"
            ))),
        }
    }
    pub fn save(
        &self,
        workspace: &Path,
        mut query: SavedQuery,
        overwrite: bool,
    ) -> Result<SavedQuery> {
        let ws = self.catalog.workspace(workspace)?;
        validate_name(&query.name)?;
        validate_name(&query.db)?;
        if query
            .sql
            .lines()
            .any(|s| s.trim_start().starts_with("-- @"))
        {
            return Err(RuntimeError::Invalid(
                "SQL body cannot contain query metadata directives".into(),
            ));
        }
        let path = ws
            .queries_dir
            .join(&query.db)
            .join(format!("{}.sql", query.name));
        let _guard = lock_file(&ws.queries_dir.join(".queries.lock"))?;
        if path.exists() && !overwrite {
            return Err(RuntimeError::Invalid(
                "query exists; explicit overwrite required".into(),
            ));
        }
        query.path = path.clone();
        query.workspace = ws.home.clone();
        query.saved_at = Some(chrono::Utc::now().to_rfc3339());
        if !query.schema_sources.is_empty() {
            query.source_fingerprint = Some(self.fingerprint(&query)?.digest);
        }
        atomic_write(&path, query.render()?.as_bytes())?;
        Ok(query)
    }
    pub fn remove(&self, name: &str, workspace: Option<&Path>) -> Result<()> {
        let q = self.describe(name, workspace)?;
        let _guard = lock_file(
            &self
                .catalog
                .workspace(&q.workspace)?
                .queries_dir
                .join(".queries.lock"),
        )?;
        std::fs::remove_file(q.path)?;
        Ok(())
    }
    pub fn fingerprint(&self, query: &SavedQuery) -> Result<Fingerprint> {
        let mut hash = Sha256::new();
        let mut files = vec![];
        let mut sources = query.schema_sources.clone();
        sources.sort();
        for declared in sources {
            let raw = Path::new(&declared);
            let mut candidates = vec![absolute(raw, &std::env::current_dir()?)?];
            if let Some(home) = std::env::var_os("HOME") {
                candidates.push(absolute(raw, &PathBuf::from(home).join("workspace"))?);
            }
            candidates.push(absolute(
                raw,
                query.workspace.parent().unwrap_or(&query.workspace),
            )?);
            let resolved = candidates
                .iter()
                .find(|p| p.exists())
                .unwrap_or(&candidates[0]);
            if resolved.exists() && !resolved.is_file() {
                return Err(RuntimeError::Invalid(
                    "schema-source must name a file; directory hashing is not supported".into(),
                ));
            }
            if resolved.is_file() {
                let bytes = std::fs::read(resolved)?;
                hash.update(format!("<FILE:{declared}:{}>", bytes.len()));
                hash.update(&bytes);
                files.push(FingerprintFile {
                    source: declared,
                    path: resolved.clone(),
                    exists: true,
                    size: Some(bytes.len()),
                });
            } else {
                hash.update(format!("<MISSING:{declared}>"));
                files.push(FingerprintFile {
                    source: declared,
                    path: resolved.clone(),
                    exists: false,
                    size: None,
                });
            }
        }
        Ok(Fingerprint {
            digest: format!("sha256:{:x}", hash.finalize())[..23].into(),
            files,
        })
    }
    pub fn audit(&self) -> Result<Vec<serde_json::Value>> {
        self.list()?.iter().map(|q|{let current=self.fingerprint(q)?;Ok(serde_json::json!({"name":q.name,"workspace":q.workspace,"status":if q.schema_sources.is_empty(){"untracked"}else if current.files.iter().any(|s|!s.exists){"missing"}else if q.source_fingerprint.as_deref()==Some(&current.digest){"fresh"}else{"stale"},"fingerprint":current.digest}))}).collect()
    }
    pub async fn run(
        &self,
        query: &SavedQuery,
        connection: &ConnectionRecord,
        parameters: BTreeMap<String, String>,
        policy: quarry_core::ExecutionPolicy,
    ) -> Result<quarry_core::QueryOutcome> {
        let tunnels =
            crate::TunnelManager::new(self.catalog.config.base().join("rust-runtime/tunnels"));
        self.run_connected(query, connection, parameters, policy, &tunnels)
            .await
    }
    pub async fn run_connected(
        &self,
        query: &SavedQuery,
        connection: &ConnectionRecord,
        parameters: BTreeMap<String, String>,
        policy: quarry_core::ExecutionPolicy,
        tunnels: &crate::TunnelManager,
    ) -> Result<quarry_core::QueryOutcome> {
        if query.db != connection.logical_db() && query.db != connection.key {
            return Err(RuntimeError::Invalid(
                "saved query belongs to a different database".into(),
            ));
        }
        let lease = tunnels
            .open(
                connection,
                self.catalog
                    .config
                    .enabled("proxy", &connection.workspace)
                    .then_some(true),
            )
            .await?;
        let mut request = quarry_core::QueryRequest::new(&query.sql);
        request.parameters = query.parameters(&parameters)?;
        request.policy = policy;
        quarry_core::execute(&lease.spec(connection)?, request)
            .await
            .map_err(RuntimeError::from)
    }
    pub async fn validate(
        &self,
        query: &SavedQuery,
        connection: &ConnectionRecord,
        parameters: BTreeMap<String, String>,
    ) -> Result<SavedQuery> {
        let tunnels =
            crate::TunnelManager::new(self.catalog.config.base().join("rust-runtime/tunnels"));
        self.validate_connected(query, connection, parameters, &tunnels)
            .await
    }
    pub async fn validate_connected(
        &self,
        query: &SavedQuery,
        connection: &ConnectionRecord,
        parameters: BTreeMap<String, String>,
        tunnels: &crate::TunnelManager,
    ) -> Result<SavedQuery> {
        let original = std::fs::read(&query.path)?;
        let lease = tunnels
            .open(
                connection,
                self.catalog
                    .config
                    .enabled("proxy", &connection.workspace)
                    .then_some(true),
            )
            .await?;
        let mut request = quarry_core::QueryRequest::new(format!("EXPLAIN {}", query.sql));
        request.parameters = query.parameters(&parameters)?;
        quarry_core::execute(&lease.spec(connection)?, request)
            .await
            .map_err(RuntimeError::from)?;
        let ws = self.catalog.workspace(&query.workspace)?;
        let _guard = lock_file(&ws.queries_dir.join(".queries.lock"))?;
        if std::fs::read(&query.path)? != original {
            return Err(RuntimeError::Operation(
                "query changed during validation; the edited file was preserved".into(),
            ));
        }
        let mut q = query.clone();
        q.last_validated = Some(chrono::Utc::now().to_rfc3339());
        atomic_write(&q.path, q.render()?.as_bytes())?;
        Ok(q)
    }
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Fingerprint {
    pub digest: String,
    pub files: Vec<FingerprintFile>,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct FingerprintFile {
    pub source: String,
    pub path: PathBuf,
    pub exists: bool,
    pub size: Option<usize>,
}
fn sql_files(
    root: &Path,
    workspace: &Path,
    inventory: &mut QueryInventory,
    issue_limit: usize,
) -> Vec<PathBuf> {
    let mut files = vec![];
    let mut directories = vec![root.to_path_buf()];
    while let Some(directory) = directories.pop() {
        let entries = match std::fs::read_dir(&directory) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => {
                inventory.issue(
                    QueryIssue {
                        path: directory,
                        workspace: workspace.into(),
                        code: QueryIssueCode::ReadDirectory,
                        reason: "query directory cannot be read".into(),
                    },
                    issue_limit,
                );
                continue;
            }
        };
        for entry in entries {
            let entry = match entry {
                Ok(entry) => entry,
                Err(_) => {
                    inventory.issue(
                        QueryIssue {
                            path: directory.clone(),
                            workspace: workspace.into(),
                            code: QueryIssueCode::ReadDirectory,
                            reason: "query directory entry cannot be read".into(),
                        },
                        issue_limit,
                    );
                    continue;
                }
            };
            let path = entry.path();
            match entry.file_type() {
                Ok(kind) if kind.is_dir() => directories.push(path),
                Ok(kind) if kind.is_file() && path.extension().is_some_and(|s| s == "sql") => {
                    files.push(path)
                }
                Ok(_) => {}
                Err(_) => inventory.issue(
                    QueryIssue {
                        path,
                        workspace: workspace.into(),
                        code: QueryIssueCode::ReadDirectory,
                        reason: "query directory entry type cannot be read".into(),
                    },
                    issue_limit,
                ),
            }
        }
    }
    files.sort();
    files
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn inventory_keeps_valid_queries_and_bounds_safe_malformed_file_issues() {
        let dir = tempfile::tempdir().unwrap();
        let broken = dir.path().join("broken");
        let valid = dir.path().join("valid");
        for workspace in [&broken, &valid] {
            std::fs::create_dir_all(workspace.join("queries/app")).unwrap();
        }
        std::fs::write(
            broken.join("queries/app/invalid_utf8.sql"),
            [0xff, 0xfe, b'x'],
        )
        .unwrap();
        std::fs::write(broken.join("queries/app/missing_db.sql"),"-- @name: missing_db\n-- @desc: password=fixture-secret-never-log\nSELECT 'private-body';\n").unwrap();
        std::fs::write(
            broken.join("queries/app/mismatch.sql"),
            "-- @name: other_name\n-- @db: app\nSELECT 1;\n",
        )
        .unwrap();
        std::fs::write(
            valid.join("queries/app/good.sql"),
            "-- @name: good\n-- @db: app\nSELECT 1;\n",
        )
        .unwrap();
        let store = QueryStore::new(
            WorkspaceCatalog::load(
                crate::RuntimeConfig::load(dir.path().join("config.toml")).unwrap(),
                &[broken.clone(), valid.clone()],
            )
            .unwrap(),
        );
        let inventory = store.scan().unwrap();
        assert_eq!(inventory.queries.len(), 1);
        assert_eq!(inventory.queries[0].name, "good");
        assert_eq!(inventory.issues.len(), 3);
        assert_eq!(inventory.omitted_issues, 0);
        assert!(
            inventory
                .issues
                .iter()
                .any(|issue| issue.code == QueryIssueCode::InvalidUtf8)
        );
        assert!(inventory.issues.iter().all(|issue| issue.workspace
            == store.catalog.workspace(&broken).unwrap().home
            && issue.path.starts_with(&issue.workspace)));
        let serialized = serde_json::to_string(&inventory.issues).unwrap();
        assert!(!serialized.contains("fixture-secret"));
        assert!(!serialized.contains("private-body"));
        assert_eq!(store.list().unwrap().len(), 1);
        assert_eq!(store.describe("good", None).unwrap().name, "good");
        assert!(
            store
                .describe("missing_db", Some(&broken))
                .unwrap_err()
                .to_string()
                .contains("missing @db")
        );
        assert!(
            store
                .describe("invalid_utf8", None)
                .unwrap_err()
                .to_string()
                .contains("not valid UTF-8")
        );
        assert!(
            store
                .describe("mismatch", None)
                .unwrap_err()
                .to_string()
                .contains("does not match filename")
        );
        let bounded = store.scan_with_issue_limit(1).unwrap();
        assert_eq!(bounded.queries.len(), 1);
        assert_eq!(bounded.issues.len(), 1);
        assert_eq!(bounded.omitted_issues, 2);
        let silent = store.scan_with_issue_limit(0).unwrap();
        assert!(silent.issues.is_empty());
        assert_eq!(silent.omitted_issues, 3);
        // A malformed file with the same filename in another workspace does
        // not hide the valid query, while explicit selection explains the error.
        std::fs::write(
            broken.join("queries/app/good.sql"),
            "-- @name: good\nSELECT 1;\n",
        )
        .unwrap();
        assert_eq!(
            store.describe("good", None).unwrap().workspace,
            store.catalog.workspace(&valid).unwrap().home
        );
        assert!(
            store
                .describe("good", Some(&broken))
                .unwrap_err()
                .to_string()
                .contains("missing @db")
        );
    }
    #[test]
    fn query_directory_failure_is_reported_without_hiding_other_workspaces() {
        let dir = tempfile::tempdir().unwrap();
        let broken = dir.path().join("broken");
        let valid = dir.path().join("valid");
        std::fs::create_dir_all(&broken).unwrap();
        std::fs::write(broken.join("queries"), "not a directory; private-content").unwrap();
        std::fs::create_dir_all(valid.join("queries")).unwrap();
        std::fs::write(
            valid.join("queries/good.sql"),
            "-- @name: good\n-- @db: app\nSELECT 1;\n",
        )
        .unwrap();
        let store = QueryStore::new(
            WorkspaceCatalog::load(
                crate::RuntimeConfig::load(dir.path().join("config.toml")).unwrap(),
                &[broken, valid],
            )
            .unwrap(),
        );
        let inventory = store.scan().unwrap();
        assert_eq!(inventory.queries.len(), 1);
        assert_eq!(inventory.issues.len(), 1);
        assert_eq!(inventory.issues[0].code, QueryIssueCode::ReadDirectory);
        assert!(!inventory.issues[0].reason.contains("private-content"));
        assert_eq!(store.describe("good", None).unwrap().name, "good");
    }
    #[test]
    fn fingerprint_matches_legacy_bytes_algorithm_and_ignores_modification_time() {
        let dir = tempfile::tempdir().unwrap();
        let workspace = dir.path().join("workspace");
        std::fs::create_dir_all(workspace.join("queries/app")).unwrap();
        let schema = dir.path().join("schema.sql");
        std::fs::write(&schema, b"CREATE TABLE demo(id int);\n").unwrap();
        let path = workspace.join("queries/app/legacy.sql");
        std::fs::write(&path,"-- @name: legacy\n-- @db: app\n-- @schema-source: schema.sql\n-- @source-fingerprint: sha256:cacc143885bce0e3\nSELECT 1;\n").unwrap();
        let catalog = WorkspaceCatalog::load(
            crate::RuntimeConfig::load(dir.path().join("config.toml")).unwrap(),
            std::slice::from_ref(&workspace),
        )
        .unwrap();
        let store = QueryStore::new(catalog);
        let mut query = store.describe("legacy", None).unwrap();
        let original = store.fingerprint(&query).unwrap();
        assert_eq!(original.digest, "sha256:cacc143885bce0e3");
        assert_eq!(store.audit().unwrap()[0]["status"], "fresh");
        let mtime = std::fs::FileTimes::new()
            .set_modified(std::time::UNIX_EPOCH + std::time::Duration::from_secs(1));
        std::fs::File::options()
            .write(true)
            .open(&schema)
            .unwrap()
            .set_times(mtime)
            .unwrap();
        assert_eq!(store.fingerprint(&query).unwrap().digest, original.digest);
        query.schema_sources = vec!["schema.sql".into(), "missing.sql".into()];
        // Computed from the previous Python implementation's exact framing:
        // sorted names, <MISSING:name>, <FILE:name:byte-length>, then raw bytes.
        assert_eq!(
            store.fingerprint(&query).unwrap().digest,
            "sha256:85712b64c4b2507f"
        );
        query.schema_sources = vec![dir.path().to_string_lossy().into()];
        assert!(store.fingerprint(&query).is_err());
    }
    #[test]
    fn query_roundtrip_and_ambiguity() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a");
        let b = dir.path().join("b");
        std::fs::create_dir_all(&a).unwrap();
        std::fs::create_dir_all(&b).unwrap();
        let config = crate::RuntimeConfig::load(dir.path().join("config.toml")).unwrap();
        let catalog = WorkspaceCatalog::load(config, &[a.clone(), b.clone()]).unwrap();
        let store = QueryStore::new(catalog);
        for ws in [&a, &b] {
            let q = SavedQuery {
                name: "sample".into(),
                db: "app".into(),
                desc: "hello".into(),
                tags: vec![],
                params: vec![QueryParameter {
                    name: "id".into(),
                    r#type: "int".into(),
                    required: true,
                    default: None,
                }],
                schema_sources: vec![],
                source_fingerprint: None,
                saved_at: None,
                last_validated: None,
                sql: "select :'id'".into(),
                path: PathBuf::new(),
                workspace: ws.clone(),
            };
            store.save(ws, q, false).unwrap();
        }
        assert!(store.describe("sample", None).is_err());
        let q = store.describe("sample", Some(&a)).unwrap();
        assert_eq!(q.sql, "select :'id'");
        assert!(q.parameters(&BTreeMap::new()).is_err());
        assert!(store.describe("../sample", None).is_err());
    }
}
