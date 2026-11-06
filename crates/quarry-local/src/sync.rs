use crate::*;
use quarry_runtime::{TunnelManager, atomic_write};
use tokio::io::AsyncWriteExt;

impl LocalManager {
    /// Copy schema only into an explicitly local PostgreSQL target. A per-target
    /// filesystem lock serializes all processes, and a recovery journal describes
    /// interrupted rename windows instead of claiming the swap is atomic.
    pub async fn sync(
        &self,
        source: &ConnectionRecord,
        target: &ConnectionRecord,
        tunnels: &TunnelManager,
    ) -> Result<Value> {
        let mut tools = self.clone();
        if source.spec()?.engine != quarry_core::Engine::Postgres
            || target.spec()?.engine != quarry_core::Engine::Postgres
        {
            return Err(RuntimeError::Invalid(
                "schema synchronization requires PostgreSQL".into(),
            ));
        }
        let target_url = url::Url::parse(&target.url)
            .map_err(|_| RuntimeError::Invalid("invalid local target URL".into()))?;
        if target.env != "local"
            || target.ssh_host.is_some()
            || !target_url.host_str().is_some_and(super::is_loopback)
        {
            return Err(RuntimeError::Invalid(
                "schema target must be local, loopback and without SSH".into(),
            ));
        }
        let db = target_url.path().trim_start_matches('/').to_string();
        quarry_runtime::validate_name(&db)?;
        if db.len() > 54 {
            return Err(RuntimeError::Invalid(
                "local database name leaves no room for staging suffix".into(),
            ));
        }
        std::fs::create_dir_all(&self.state_dir)?;
        let key = sync_key(&target_url);
        let _lock = self.state_lock(&format!("sync-{key}")).await?;
        let journal = self.state_dir.join(format!("sync-{key}.json"));
        if journal.exists() {
            let old: Value = serde_json::from_slice(&std::fs::read(&journal)?)?;
            if old["state"] != "complete" && old["state"] != "rolled_back" {
                return Err(RuntimeError::Operation(format!(
                    "an interrupted schema sync requires recovery; inspect {}",
                    journal.display()
                )));
            }
        }
        let lease = tunnels
            .open(
                source,
                self.config
                    .enabled("proxy", &source.workspace)
                    .then_some(true),
            )
            .await?;
        let mut source_url = url::Url::parse(&source.url)
            .map_err(|_| RuntimeError::Invalid("invalid schema source URL".into()))?;
        if let Some(endpoint) = lease.endpoint() {
            source_url
                .query_pairs_mut()
                .append_pair("hostaddr", &endpoint.host)
                .append_pair("port", &endpoint.port.to_string());
        }
        let mut admin = target_url.clone();
        admin.set_path("/postgres");
        let staging = format!("{db}__staging");
        let previous = format!("{db}__prev");
        let mut staging_url = target_url.clone();
        staging_url.set_path(&format!("/{staging}"));
        let server = tools
            .pg_query(&source_url, "SHOW server_version_num")
            .await?;
        let major = server.trim().parse::<u32>().map_err(|_| {
            RuntimeError::Operation("cannot identify PostgreSQL source version".into())
        })? / 10000;
        let dump_bin = self.select_pg_dump(major).await?;
        // Use the matching psql package for newer dump security guards. An
        // explicitly selected psql remains the caller's choice.
        if self.psql.as_path() == std::path::Path::new("psql")
            && let Some(binary) = resolve_executable(&dump_bin)
            && let Some(directory) = binary.parent()
            && directory.join("psql").is_file()
        {
            tools.psql = directory.join("psql");
        }
        let source_db = tools
            .pg_query(&source_url, "SELECT current_database()")
            .await?
            .trim()
            .to_string();
        let (safe_url, env) = pg_command_connection(&source_url)?;
        let mut dump_cmd = Command::new(dump_bin);
        dump_cmd
            .args(["--schema-only", "--no-owner", "--no-privileges", &safe_url])
            .envs(env)
            .kill_on_drop(true);
        let dump = tokio::time::timeout(Duration::from_secs(120), dump_cmd.output())
            .await
            .map_err(|_| RuntimeError::Operation("pg_dump timed out".into()))??;
        if !dump.status.success() {
            return Err(RuntimeError::Operation(
                "pg_dump failed; source and local live database were not modified".into(),
            ));
        }
        let dump = String::from_utf8(dump.stdout)
            .map_err(|_| RuntimeError::Operation("schema dump was not valid UTF-8".into()))?;
        let dump = sanitize_dump(&dump, &source_db, &staging)?;
        let exists = tools
            .pg_query(
                &admin,
                &format!("SELECT 1 FROM pg_database WHERE datname='{}'", db),
            )
            .await?
            .trim()
            == "1";
        tools
            .pg_query(
                &admin,
                &format!("DROP DATABASE IF EXISTS \"{staging}\" WITH (FORCE)"),
            )
            .await?;
        tools
            .pg_query(
                &admin,
                &format!("CREATE DATABASE \"{staging}\" TEMPLATE template0"),
            )
            .await?;
        let mut final_dump = String::new();
        if !dump
            .lines()
            .any(|line| line.trim_start().starts_with("CREATE SCHEMA public"))
        {
            let public = tools
                .pg_query(
                    &source_url,
                    "SELECT 1 FROM pg_namespace WHERE nspname='public'",
                )
                .await?;
            if public.trim() != "1" {
                final_dump.push_str("DROP SCHEMA IF EXISTS public CASCADE;\n");
            }
        }
        final_dump.push_str(&dump);
        if let Err(error) = tools.pg_script(&staging_url, &final_dump).await {
            let _ = tools
                .pg_query(
                    &admin,
                    &format!("DROP DATABASE IF EXISTS \"{staging}\" WITH (FORCE)"),
                )
                .await;
            return Err(error);
        }
        let write_journal = |state: &str| {
            atomic_write(
                &journal,
                &serde_json::to_vec_pretty(
                    &json!({"state":state,"database":db,"staging":staging,"previous":previous,"target":target.key,"had_live":exists}),
                )?,
            )
        };
        write_journal("prepared")?;
        tools.pg_query(&admin,&format!("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('{db}','{previous}') AND pid<>pg_backend_pid()")).await?;
        tools
            .pg_query(
                &admin,
                &format!("DROP DATABASE IF EXISTS \"{previous}\" WITH (FORCE)"),
            )
            .await?;
        if exists {
            tools
                .pg_query(
                    &admin,
                    &format!("ALTER DATABASE \"{db}\" RENAME TO \"{previous}\""),
                )
                .await?;
            write_journal("main_renamed")?;
        }
        if let Err(error) = tools
            .pg_query(
                &admin,
                &format!("ALTER DATABASE \"{staging}\" RENAME TO \"{db}\""),
            )
            .await
        {
            if exists
                && tools
                    .pg_query(
                        &admin,
                        &format!("ALTER DATABASE \"{previous}\" RENAME TO \"{db}\""),
                    )
                    .await
                    .is_err()
            {
                write_journal("recovery_required")?;
                return Err(RuntimeError::Operation(format!(
                    "schema activation failed and automatic rollback failed; recovery journal: {}",
                    journal.display()
                )));
            }
            write_journal("rolled_back")?;
            return Err(error);
        }
        write_journal("complete")?;
        Ok(json!({"db":db,"prev":exists.then_some(previous),"journal":journal,"schema_only":true}))
    }
    /// Export authorized PostgreSQL structure without connection credentials or
    /// business rows. The host performs IAM/resource checks before calling this.
    /// Consumers must import only into an explicitly local staging database.
    pub async fn export_schema(
        &self,
        source: &ConnectionRecord,
        tunnels: &TunnelManager,
    ) -> Result<String> {
        use tokio::io::AsyncReadExt;
        if source.spec()?.engine != quarry_core::Engine::Postgres {
            return Err(RuntimeError::Invalid(
                "schema export requires PostgreSQL".into(),
            ));
        }
        let lease = tunnels
            .open(
                source,
                self.config
                    .enabled("proxy", &source.workspace)
                    .then_some(true),
            )
            .await?;
        let mut source_url = url::Url::parse(&source.url)
            .map_err(|_| RuntimeError::Invalid("invalid schema source URL".into()))?;
        if let Some(endpoint) = lease.endpoint() {
            source_url
                .query_pairs_mut()
                .append_pair("hostaddr", &endpoint.host)
                .append_pair("port", &endpoint.port.to_string());
        }
        let server = self
            .pg_query(&source_url, "SHOW server_version_num")
            .await?;
        let major = server.trim().parse::<u32>().map_err(|_| {
            RuntimeError::Operation("cannot identify PostgreSQL source version".into())
        })? / 10000;
        let dump_bin = self.select_pg_dump(major).await?;
        let public_exists = self
            .pg_query(
                &source_url,
                "SELECT 1 FROM pg_namespace WHERE nspname='public'",
            )
            .await?
            .trim()
            == "1";
        let (safe_url, env) = pg_command_connection(&source_url)?;
        let mut child = Command::new(dump_bin)
            .args(["--schema-only", "--no-owner", "--no-privileges", &safe_url])
            .envs(env)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| RuntimeError::Operation("cannot read schema export".into()))?;
        let read = async {
            let mut bytes = Vec::new();
            stdout.take(8_388_609).read_to_end(&mut bytes).await?;
            if bytes.len() > 8_388_608 {
                return Err(RuntimeError::Invalid("schema export exceeds 8 MiB".into()));
            }
            Ok::<_, RuntimeError>(bytes)
        };
        let wait = async { child.wait().await.map_err(RuntimeError::from) };
        let (bytes, status) = tokio::time::timeout(Duration::from_secs(120), async {
            tokio::try_join!(read, wait)
        })
        .await
        .map_err(|_| RuntimeError::Operation("schema export timed out".into()))??;
        if !status.success() {
            return Err(RuntimeError::Operation(
                "schema export failed; source was not modified".into(),
            ));
        }
        let raw = String::from_utf8(bytes)
            .map_err(|_| RuntimeError::Invalid("schema export was not UTF-8".into()))?;
        let schema = transport_schema(&raw, true)?;
        let creates_public =
            regex::Regex::new(r#"(?im)^CREATE\s+SCHEMA\s+(?:public|"public")[;\s]"#)
                .unwrap()
                .is_match(&schema);
        let mut normalized = String::from("DROP SCHEMA IF EXISTS public CASCADE;\n");
        if public_exists && !creates_public {
            normalized.push_str("CREATE SCHEMA public;\n");
        }
        normalized.push_str(&schema);
        if normalized.len() > 8_388_608 {
            return Err(RuntimeError::Invalid("schema export exceeds 8 MiB".into()));
        }
        Ok(normalized)
    }
    /// Import a schema supplied by a trusted authorized host. No arbitrary
    /// psql commands or database-level operations are accepted. Activation uses
    /// the same per-target lock, staging and rollback journal as local sync.
    pub async fn import_schema(&self, target: &ConnectionRecord, schema: &str) -> Result<Value> {
        if target.spec()?.engine != quarry_core::Engine::Postgres {
            return Err(RuntimeError::Invalid(
                "schema import requires PostgreSQL".into(),
            ));
        }
        let tools = self.clone();
        let dump = transport_schema(schema, false)?;
        let target_url = url::Url::parse(&target.url)
            .map_err(|_| RuntimeError::Invalid("invalid local target URL".into()))?;
        if target.env != "local"
            || target.ssh_host.is_some()
            || !target_url.host_str().is_some_and(super::is_loopback)
        {
            return Err(RuntimeError::Invalid(
                "schema target must be local, loopback and without SSH".into(),
            ));
        }
        let db = target_url.path().trim_start_matches('/').to_string();
        quarry_runtime::validate_name(&db)?;
        if db.len() > 54 {
            return Err(RuntimeError::Invalid(
                "local database name leaves no room for staging suffix".into(),
            ));
        }
        std::fs::create_dir_all(&self.state_dir)?;
        let key = sync_key(&target_url);
        let _lock = self.state_lock(&format!("sync-{key}")).await?;
        let journal = self.state_dir.join(format!("sync-{key}.json"));
        if journal.exists() {
            let old: Value = serde_json::from_slice(&std::fs::read(&journal)?)?;
            if old["state"] != "complete" && old["state"] != "rolled_back" {
                return Err(RuntimeError::Operation(format!(
                    "an interrupted schema sync requires recovery; inspect {}",
                    journal.display()
                )));
            }
        }
        let mut admin = target_url.clone();
        admin.set_path("/postgres");
        let staging = format!("{db}__staging");
        let previous = format!("{db}__prev");
        let mut staging_url = target_url.clone();
        staging_url.set_path(&format!("/{staging}"));
        let exists = tools
            .pg_query(
                &admin,
                &format!("SELECT 1 FROM pg_database WHERE datname='{}'", db),
            )
            .await?
            .trim()
            == "1";
        tools
            .pg_query(
                &admin,
                &format!("DROP DATABASE IF EXISTS \"{staging}\" WITH (FORCE)"),
            )
            .await?;
        tools
            .pg_query(
                &admin,
                &format!("CREATE DATABASE \"{staging}\" TEMPLATE template0"),
            )
            .await?;
        if let Err(error) = tools.pg_script(&staging_url, &dump).await {
            let _ = tools
                .pg_query(
                    &admin,
                    &format!("DROP DATABASE IF EXISTS \"{staging}\" WITH (FORCE)"),
                )
                .await;
            return Err(error);
        }
        let write_journal = |state: &str| {
            atomic_write(
                &journal,
                &serde_json::to_vec_pretty(
                    &json!({"state":state,"database":db,"staging":staging,"previous":previous,"target":target.key,"had_live":exists}),
                )?,
            )
        };
        write_journal("prepared")?;
        tools.pg_query(&admin,&format!("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('{db}','{previous}') AND pid<>pg_backend_pid()")).await?;
        tools
            .pg_query(
                &admin,
                &format!("DROP DATABASE IF EXISTS \"{previous}\" WITH (FORCE)"),
            )
            .await?;
        if exists {
            tools
                .pg_query(
                    &admin,
                    &format!("ALTER DATABASE \"{db}\" RENAME TO \"{previous}\""),
                )
                .await?;
            write_journal("main_renamed")?;
        }
        if let Err(error) = tools
            .pg_query(
                &admin,
                &format!("ALTER DATABASE \"{staging}\" RENAME TO \"{db}\""),
            )
            .await
        {
            if exists
                && tools
                    .pg_query(
                        &admin,
                        &format!("ALTER DATABASE \"{previous}\" RENAME TO \"{db}\""),
                    )
                    .await
                    .is_err()
            {
                write_journal("recovery_required")?;
                return Err(RuntimeError::Operation(format!(
                    "schema activation failed and automatic rollback failed; recovery journal: {}",
                    journal.display()
                )));
            }
            write_journal("rolled_back")?;
            return Err(error);
        }
        write_journal("complete")?;
        Ok(json!({"db":db,"prev":exists.then_some(previous),"journal":journal,"schema_only":true}))
    }
    pub async fn recover_sync(&self, target: &ConnectionRecord) -> Result<Value> {
        if target.spec()?.engine != quarry_core::Engine::Postgres {
            return Err(RuntimeError::Invalid(
                "schema recovery requires PostgreSQL".into(),
            ));
        }
        let url = url::Url::parse(&target.url)
            .map_err(|_| RuntimeError::Invalid("invalid local target".into()))?;
        if target.env != "local"
            || target.ssh_host.is_some()
            || !url.host_str().is_some_and(super::is_loopback)
        {
            return Err(RuntimeError::Invalid("recovery target is not local".into()));
        }
        let db = url.path().trim_start_matches('/');
        quarry_runtime::validate_name(db)?;
        let key = sync_key(&url);
        let _lock = self.state_lock(&format!("sync-{key}")).await?;
        let journal = self.state_dir.join(format!("sync-{key}.json"));
        if !journal.is_file() {
            return Err(RuntimeError::NotFound(
                "no schema synchronization journal for this target".into(),
            ));
        }
        let mut record: Value = serde_json::from_slice(&std::fs::read(&journal)?)?;
        let staging = format!("{db}__staging");
        let previous = format!("{db}__prev");
        if record["database"] != db
            || record["staging"] != staging
            || record["previous"] != previous
        {
            return Err(RuntimeError::Invalid(
                "schema recovery journal does not match target".into(),
            ));
        }
        if record["state"] == "complete" || record["state"] == "rolled_back" {
            return Ok(
                json!({"db":db,"recovered":false,"state":record["state"],"journal":journal}),
            );
        }
        if !["prepared", "main_renamed", "recovery_required"]
            .iter()
            .any(|s| record["state"] == *s)
        {
            return Err(RuntimeError::Invalid(
                "unknown schema recovery journal state".into(),
            ));
        }
        let mut admin = url.clone();
        admin.set_path("/postgres");
        let current = self
            .pg_query(
                &admin,
                &format!("SELECT 1 FROM pg_database WHERE datname='{db}'"),
            )
            .await?;
        let stage_exists = self
            .pg_query(
                &admin,
                &format!("SELECT 1 FROM pg_database WHERE datname='{staging}'"),
            )
            .await?
            .trim()
            == "1";
        let mut restored = false;
        let state = if current.trim() == "1" {
            // A successful rename or rollback may precede a crash writing the
            // journal. The live database is authoritative and never overwritten.
            if stage_exists || record["state"] == "prepared" {
                "rolled_back"
            } else {
                "complete"
            }
        } else {
            let old_exists = self
                .pg_query(
                    &admin,
                    &format!("SELECT 1 FROM pg_database WHERE datname='{previous}'"),
                )
                .await?
                .trim()
                == "1";
            if old_exists {
                self.pg_query(&admin,&format!("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{previous}' AND pid<>pg_backend_pid()")).await?;
                self.pg_query(
                    &admin,
                    &format!("ALTER DATABASE \"{previous}\" RENAME TO \"{db}\""),
                )
                .await?;
                restored = true;
            } else if record["had_live"] != false {
                return Err(RuntimeError::Operation("previous live database is missing; recovery will not activate unverified staging data".into()));
            }
            "rolled_back"
        };
        if state == "rolled_back" && stage_exists {
            self.pg_query(
                &admin,
                &format!("DROP DATABASE IF EXISTS \"{staging}\" WITH (FORCE)"),
            )
            .await?;
        }
        record["state"] = json!(state);
        atomic_write(&journal, &serde_json::to_vec_pretty(&record)?)?;
        Ok(json!({"db":db,"recovered":restored,"state":state,"journal":journal}))
    }
    async fn select_pg_dump(&self, server_major: u32) -> Result<PathBuf> {
        let mut candidates = vec![self.pg_dump.clone()];
        if self.pg_dump.as_path() == std::path::Path::new("pg_dump") {
            if let Some(psql) = resolve_executable(&self.psql) {
                let sibling = psql.with_file_name("pg_dump");
                if sibling.is_file() {
                    candidates.insert(0, sibling);
                }
            }
            for root in ["/opt/homebrew/opt", "/usr/local/opt", "/usr/lib/postgresql"] {
                if let Ok(entries) = std::fs::read_dir(root) {
                    let mut entries = entries.flatten().map(|e| e.path()).collect::<Vec<_>>();
                    entries.sort();
                    entries.reverse();
                    for entry in entries {
                        let candidate = entry.join("bin/pg_dump");
                        if candidate.is_file() {
                            candidates.push(candidate);
                        }
                    }
                }
            }
        }
        let version_pattern = regex::Regex::new(r"PostgreSQL\)?\s+(\d+)").unwrap();
        for candidate in candidates {
            let mut command = Command::new(&candidate);
            command.arg("--version").kill_on_drop(true);
            let output = tokio::time::timeout(Duration::from_secs(5), command.output()).await;
            let Ok(Ok(output)) = output else {
                continue;
            };
            if !output.status.success() {
                continue;
            }
            let text = String::from_utf8_lossy(&output.stdout);
            let major = version_pattern
                .captures(&text)
                .and_then(|c| c[1].parse::<u32>().ok());
            if major.is_some_and(|v| v >= server_major) {
                return Ok(resolve_executable(&candidate).unwrap_or(candidate));
            }
        }
        Err(RuntimeError::Operation(format!(
            "pg_dump compatible with PostgreSQL {server_major} is unavailable"
        )))
    }
    async fn pg_query(&self, url: &url::Url, sql: &str) -> Result<String> {
        self.pg_script(url, sql).await
    }
    async fn pg_script(&self, url: &url::Url, sql: &str) -> Result<String> {
        let (safe, env) = pg_command_connection(url)?;
        let mut command = Command::new(&self.psql);
        command
            .args([
                "--no-psqlrc",
                "--set",
                "ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--quiet",
                "--dbname",
                &safe,
            ])
            .envs(env)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        let mut child = command.spawn()?;
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| RuntimeError::Operation("cannot write schema SQL to psql".into()))?;
        // Drain output while feeding stdin: large dumps or server notices must
        // not deadlock a full stderr pipe before input has finished writing.
        let write = async {
            stdin.write_all(sql.as_bytes()).await?;
            drop(stdin);
            Ok::<_, std::io::Error>(())
        };
        let operation =
            async { tokio::try_join!(write, child.wait_with_output()).map(|(_, output)| output) };
        let output = tokio::time::timeout(Duration::from_secs(120), operation)
            .await
            .map_err(|_| {
                RuntimeError::Operation("PostgreSQL schema operation timed out".into())
            })??;
        if !output.status.success() {
            return Err(RuntimeError::Operation(
                "PostgreSQL schema operation failed; connection credentials omitted".into(),
            ));
        }
        Ok(String::from_utf8_lossy(&output.stdout).into())
    }
}
fn resolve_executable(path: &std::path::Path) -> Option<PathBuf> {
    if path.components().count() > 1 {
        return path.canonicalize().ok();
    }
    std::env::var_os("PATH")
        .and_then(|paths| {
            std::env::split_paths(&paths)
                .map(|directory| directory.join(path))
                .find(|candidate| candidate.is_file())
        })
        .and_then(|p| p.canonicalize().ok())
}
fn sync_key(target: &url::Url) -> String {
    // Credentials/workspace aliases must not allow concurrent swaps of the
    // same actual local database or hide its recovery journal after rotation.
    format!(
        "{:x}",
        Sha256::digest(
            format!(
                "loopback:{}:{}",
                target.port().unwrap_or(5432),
                target.path()
            )
            .as_bytes()
        )
    )
}
fn pg_command_connection(url: &url::Url) -> Result<(String, BTreeMap<String, String>)> {
    let mut env = BTreeMap::new();
    let mut safe = url.clone();
    if let Some(password) = url.password() {
        env.insert("PGPASSWORD".into(), percent_decode(password)?);
        safe.set_password(None)
            .map_err(|_| RuntimeError::Invalid("invalid PostgreSQL URL".into()))?;
    }
    let mut pairs = vec![];
    for (key, value) in url.query_pairs() {
        if key == "password" {
            env.insert("PGPASSWORD".into(), value.into_owned());
        } else if key == "sslpassword" {
            return Err(RuntimeError::Invalid("encrypted client-key passwords must use a libpq service file; secret URL options are never put in process arguments".into()));
        } else {
            pairs.push((key.into_owned(), value.into_owned()));
        }
    }
    if url.query().is_some() {
        safe.set_query(None);
        if !pairs.is_empty() {
            safe.query_pairs_mut().extend_pairs(pairs);
        }
    }
    Ok((safe.to_string(), env))
}
fn percent_decode(value: &str) -> Result<String> {
    let mut bytes = vec![];
    let source = value.as_bytes();
    let mut i = 0;
    while i < source.len() {
        if source[i] == b'%' && i + 2 < source.len() {
            let hex = std::str::from_utf8(&source[i + 1..i + 3])
                .map_err(|_| RuntimeError::Invalid("invalid URL escape".into()))?;
            bytes.push(
                u8::from_str_radix(hex, 16)
                    .map_err(|_| RuntimeError::Invalid("invalid URL escape".into()))?,
            );
            i += 3;
        } else {
            bytes.push(source[i]);
            i += 1;
        }
    }
    String::from_utf8(bytes).map_err(|_| RuntimeError::Invalid("password is not UTF-8".into()))
}
/// PostgreSQL plain-format structure, never an executable psql program.
fn transport_schema(schema: &str, from_dump: bool) -> Result<String> {
    if schema.is_empty() || schema.len() > 8_388_608 || schema.contains('\0') {
        return Err(RuntimeError::Invalid(
            "schema must be nonempty UTF-8, up to 8 MiB".into(),
        ));
    }
    let database =
        regex::Regex::new(r"(?i)^(?:CREATE|ALTER|DROP|COMMENT ON)\s+DATABASE\b").unwrap();
    let timeout = regex::Regex::new(r"(?i)^SET\s+transaction_timeout\s*=\s*0;").unwrap();
    let mut out = String::new();
    for line in schema.lines() {
        let text = line.trim_start();
        if text.starts_with('\\') {
            if from_dump && (text.starts_with("\\restrict ") || text.starts_with("\\unrestrict ")) {
                continue;
            }
            return Err(RuntimeError::Invalid(
                "schema contains a psql command".into(),
            ));
        }
        if database.is_match(text) {
            if from_dump {
                continue;
            }
            return Err(RuntimeError::Invalid(
                "schema contains a database-level operation".into(),
            ));
        }
        if timeout.is_match(text) {
            continue;
        }
        out.push_str(line);
        out.push('\n');
    }
    Ok(out)
}
fn sanitize_dump(dump: &str, source_db: &str, target_db: &str) -> Result<String> {
    quarry_runtime::validate_name(target_db)?;
    let mut out = String::new();
    let create_database = regex::Regex::new(r"(?i)^CREATE\s+DATABASE\b").unwrap();
    let unsupported_timeout =
        regex::Regex::new(r"(?i)^SET\s+transaction_timeout\s*=\s*0;").unwrap();
    for line in dump.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("\\connect")
            || create_database.is_match(trimmed)
            || unsupported_timeout.is_match(trimmed)
        {
            continue;
        }
        let mut line = line.to_string();
        for prefix in ["COMMENT ON DATABASE", "ALTER DATABASE"] {
            for original in [source_db.to_string(), format!("\"{source_db}\"")] {
                let from = format!("{prefix} {original}");
                if line.starts_with(&from) {
                    line = line.replacen(&from, &format!("{prefix} \"{target_db}\""), 1);
                }
            }
        }
        out.push_str(&line);
        out.push('\n');
    }
    Ok(out)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn transport_dump_removes_database_scope_and_rejects_client_programs() {
        let schema = transport_schema("\\restrict nonce\nCREATE DATABASE remote;\nALTER DATABASE remote SET search_path=evil;\nCREATE TABLE test(id integer);\n\\unrestrict nonce\n", true).unwrap();
        assert_eq!(schema, "CREATE TABLE test(id integer);\n");
        for bad in [
            "\\! touch /tmp/no",
            "  \\copy rows FROM PROGRAM 'bad'",
            "DROP DATABASE other;",
            "ALTER DATABASE other RENAME TO stolen;",
            "",
            "SELECT 1;\0",
        ] {
            assert!(transport_schema(bad, false).is_err(), "{bad:?}");
        }
        assert!(transport_schema(&"x".repeat(8_388_609), false).is_err());
    }
    #[tokio::test]
    async fn transport_import_rejects_nonlocal_targets_before_invoking_tools() {
        let dir = tempfile::tempdir().unwrap();
        let manager = LocalManager::new(
            RuntimeConfig::load(dir.path().join("config.toml")).unwrap(),
            dir.path().join("state"),
        )
        .with_tools(Some(dir.path().join("must-not-run")), None, None);
        let base = json!({"key":"app","url":"postgres://u:p@db.example/app","engine":"postgres","env":"local","workspace":dir.path()});
        for record in [
            base.clone(),
            {
                let mut r = base.clone();
                r["url"] = json!("postgres://u:p@localhost/app");
                r["env"] = json!("prod");
                r
            },
            {
                let mut r = base;
                r["url"] = json!("postgres://u:p@localhost/app");
                r["ssh_host"] = json!("jump.example");
                r
            },
        ] {
            let record: ConnectionRecord = serde_json::from_value(record).unwrap();
            let error = manager
                .import_schema(&record, "CREATE TABLE t(id integer);")
                .await
                .unwrap_err();
            assert!(
                error.to_string().contains("target must be local"),
                "{error}"
            );
        }
        assert!(!manager.state_dir.exists());
    }
    #[test]
    fn credentials_are_not_in_pg_arguments() {
        let url = url::Url::parse("postgres://test:secret%21@localhost/db").unwrap();
        let (args, env) = pg_command_connection(&url).unwrap();
        assert!(!args.contains("secret"));
        assert_eq!(env["PGPASSWORD"], "secret!");
    }
    #[test]
    fn dump_does_not_retarget_the_source() {
        let text=sanitize_dump("\\connect source\nCREATE DATABASE source;\nCOMMENT ON DATABASE source IS 'x';\n\\restrict abc\n","source","local_stage").unwrap();
        assert!(!text.contains("\\connect"));
        assert!(!text.contains("CREATE DATABASE"));
        assert!(text.contains("COMMENT ON DATABASE \"local_stage\""));
        assert!(text.contains("\\restrict"));
    }
}

#[cfg(test)]
mod compensation_tests {
    use super::*;

    #[tokio::test]
    async fn second_rename_failure_restores_the_original_database() {
        let dir = tempfile::tempdir().unwrap();
        let psql = dir.path().join("psql");
        let dump = dir.path().join("pg_dump");
        let log = dir.path().join("sql.log");
        write_executable_fixture(
            &psql,
            &format!(
                r#"#!/bin/sh
sql=$(cat)
printf '%s\n' "$sql" >> '{}'
case "$sql" in
'SHOW server_version_num') echo 160010;;
'SELECT current_database()') echo source;;
*'SELECT 1 FROM pg_database'*) echo 1;;
*"SELECT 1 FROM pg_namespace"*) echo 1;;
'ALTER DATABASE "app__staging" RENAME TO "app"') exit 1;;
*) :;;
esac
"#,
                log.display()
            ),
        );
        write_executable_fixture(
            &dump,
            "#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'pg_dump (PostgreSQL) 16.10'; else echo 'CREATE TABLE example(id integer);'; fi\n",
        );
        let config = RuntimeConfig::load(dir.path().join("config.toml")).unwrap();
        let mut manager = LocalManager::new(config, dir.path().join("state"));
        manager.psql = psql;
        manager.pg_dump = dump;
        let source:ConnectionRecord=serde_json::from_value(json!({"key":"app_dev","db":"app","url":"postgres://test:secret@localhost/source","engine":"postgres","env":"dev","workspace":dir.path()})).unwrap();
        let mut target = source.clone();
        target.env = "local".into();
        target.url = "postgres://test:secret@localhost/app".into();
        target.key = "app_local".into();
        let tunnels = quarry_runtime::TunnelManager::new(dir.path().join("tunnels"));
        let error = manager.sync(&source, &target, &tunnels).await.unwrap_err();
        assert!(
            matches!(
                &error,
                RuntimeError::Operation(message)
                    if message == "PostgreSQL schema operation failed; connection credentials omitted"
            ),
            "expected the failed staging rename, got {error:?}"
        );
        let sql = std::fs::read_to_string(log).unwrap();
        assert!(sql.contains("ALTER DATABASE \"app\" RENAME TO \"app__prev\""));
        assert!(sql.contains("ALTER DATABASE \"app__staging\" RENAME TO \"app\""));
        assert!(sql.contains("ALTER DATABASE \"app__prev\" RENAME TO \"app\""));
        let journal = std::fs::read_dir(dir.path().join("state"))
            .unwrap()
            .flatten()
            .find(|e| e.path().extension().is_some_and(|x| x == "json"))
            .unwrap();
        let value: Value = serde_json::from_slice(&std::fs::read(journal.path()).unwrap()).unwrap();
        assert_eq!(value["state"], "rolled_back");
    }
    #[tokio::test]
    async fn interrupted_swap_recovery_restores_previous_and_converges_journal() {
        let dir = tempfile::tempdir().unwrap();
        let psql = dir.path().join("psql");
        let log = dir.path().join("sql.log");
        write_executable_fixture(
            &psql,
            &format!(
                r#"#!/bin/sh
sql=$(cat)
printf '%s\n' "$sql" >> '{}'
case "$sql" in
*"datname='app'") :;;
*"datname='app__staging'") echo 1;;
*"datname='app__prev'") echo 1;;
*) :;;
esac
"#,
                log.display()
            ),
        );
        let mut manager = LocalManager::new(
            RuntimeConfig::load(dir.path().join("config.toml")).unwrap(),
            dir.path().join("state"),
        );
        manager.psql = psql;
        let target:ConnectionRecord=serde_json::from_value(json!({"key":"app_local","url":"postgres://user:secret@localhost:5444/app","engine":"postgres","env":"local","workspace":dir.path()})).unwrap();
        let key = sync_key(&url::Url::parse(&target.url).unwrap());
        let journal = manager.state_dir.join(format!("sync-{key}.json"));
        atomic_write(&journal,&serde_json::to_vec(&json!({"state":"recovery_required","database":"app","staging":"app__staging","previous":"app__prev","target":"app_local","had_live":true})).unwrap()).unwrap();
        let result = manager.recover_sync(&target).await.unwrap();
        assert_eq!(result["state"], "rolled_back");
        assert_eq!(result["recovered"], true);
        let commands = std::fs::read_to_string(&log).unwrap();
        assert!(commands.contains("ALTER DATABASE \"app__prev\" RENAME TO \"app\""));
        assert!(commands.contains("DROP DATABASE IF EXISTS \"app__staging\""));
        let mut rotated = target.clone();
        rotated.url = "postgres://user:rotated@127.0.0.1:5444/app".into();
        assert_eq!(
            manager.recover_sync(&rotated).await.unwrap()["recovered"],
            false
        );
        assert_eq!(std::fs::read_to_string(&log).unwrap(), commands);
    }
}
