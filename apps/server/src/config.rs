use quarry_platform::HostedResource;
use serde::Deserialize;
use std::io::Write;
#[cfg(unix)]
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

/// The public one-file deployment format. The advanced SDK-compatible format
/// remains available for platforms which already maintain connection files.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CompactConfig {
    #[serde(default = "listen")]
    listen: String,
    #[serde(default = "default_state")]
    state_dir: PathBuf,
    auth: AuthConfig,
    databases: Vec<Database>,
    assistant_base_url: Option<String>,
    assistant_model: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Database {
    id: String,
    #[serde(default = "default_label")]
    project: String,
    db: String,
    #[serde(default = "default_label")]
    env: String,
    url: String,
    #[serde(default = "production")]
    production: bool,
    #[serde(default)]
    readers: Vec<String>,
}
fn default_state() -> PathBuf {
    "/var/lib/quarry".into()
}
fn default_label() -> String {
    "default".into()
}
fn production() -> bool {
    true
}

fn private_write(path: &Path, bytes: &[u8]) -> Result<(), String> {
    if std::fs::symlink_metadata(path).is_ok_and(|m| !m.is_file()) {
        return Err("Generated hosted configuration must not be a symlink".into());
    }
    let tmp = path.with_extension(format!("tmp-{}", uuid::Uuid::new_v4()));
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    let result = (|| {
        let mut file = options.open(&tmp)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        std::fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(tmp);
    }
    result.map_err(|_| "Cannot materialize private hosted connections".into())
}
impl CompactConfig {
    fn prepare(self) -> Result<Config, String> {
        let generated = self.state_dir.join("hosted-generated");
        let mut seen = std::collections::HashSet::new();
        let mut connections = String::new();
        let mut resources = Vec::new();
        for db in self.databases {
            if db.id.is_empty()
                || db.id.len() > 128
                || !db
                    .id
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"-_".contains(&c))
                || !seen.insert(db.id.clone())
            {
                return Err("Database IDs must be unique alphanumeric slugs".into());
            }
            if db.url.is_empty()
                || db.url.chars().any(char::is_control)
                || [&db.project, &db.db, &db.env]
                    .iter()
                    .any(|s| s.is_empty() || s.len() > 256 || s.chars().any(char::is_control))
            {
                return Err("Invalid hosted database labels or connection URL".into());
            }
            let quote = |value: &str| serde_json::to_string(value).expect("string serialization");
            connections.push_str(&format!(
                "[{}]\nurl = {}\nenv = {}\nproduction = {}\n\n",
                quote(&db.id),
                quote(&db.url),
                quote(&db.env),
                db.production
            ));
            resources.push(HostedResource {
                id: db.id.clone(),
                project: db.project,
                db: db.db,
                env: db.env,
                connection_key: db.id,
                workspace: Some(generated.clone()),
                readers: db.readers,
            });
        }
        let config = Config {
            config_path: generated.join("config.toml"),
            workspaces: vec![generated.clone()],
            state_dir: self.state_dir,
            resources,
            auth: self.auth,
            assistant_base_url: self.assistant_base_url,
            assistant_model: self.assistant_model,
            listen: self.listen,
        };
        config.validate()?;
        std::fs::create_dir_all(&config.state_dir)
            .map_err(|_| "Cannot create hosted state directory")?;
        match std::fs::symlink_metadata(&generated) {
            Ok(meta) if !meta.is_dir() => {
                return Err("Generated hosted directory must not be a symlink".into());
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let mut builder = std::fs::DirBuilder::new();
                #[cfg(unix)]
                builder.mode(0o700);
                builder
                    .create(&generated)
                    .map_err(|_| "Cannot create private hosted configuration")?;
            }
            Err(_) => return Err("Cannot inspect private hosted configuration".into()),
        }
        #[cfg(unix)]
        std::fs::set_permissions(&generated, std::fs::Permissions::from_mode(0o700))
            .map_err(|_| "Cannot protect private hosted configuration")?;
        private_write(
            &config.config_path,
            b"# Managed by quarry-server; configure databases in hosted.json.\n",
        )?;
        private_write(&generated.join("connections.toml"), connections.as_bytes())?;
        Ok(config)
    }
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub config_path: PathBuf,
    #[serde(default)]
    pub workspaces: Vec<PathBuf>,
    pub state_dir: PathBuf,
    pub resources: Vec<HostedResource>,
    pub auth: AuthConfig,
    pub assistant_base_url: Option<String>,
    pub assistant_model: Option<String>,
    #[serde(default = "listen")]
    pub listen: String,
}
fn listen() -> String {
    "0.0.0.0:8080".into()
}
#[derive(Clone, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case", deny_unknown_fields)]
pub enum AuthConfig {
    Builtin {
        #[serde(default = "admin")]
        user_id: String,
        username: String,
        password_hash: String,
        #[serde(default)]
        roles: Vec<String>,
    },
    External {
        #[serde(default)]
        cli_handoff: bool,
        verify_url: String,
        #[serde(default)]
        allow_insecure_http: bool,
        #[serde(default)]
        response: VerifyResponse,
        session_key: String,
        login_url: String,
        refresh_path: String,
        logout_path: Option<String>,
    },
}
fn admin() -> String {
    "admin".into()
}
#[derive(Clone, Copy, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VerifyResponse {
    #[default]
    Headers,
    Json,
}
impl Config {
    pub fn read(path: &Path) -> Result<Self, String> {
        let meta =
            std::fs::symlink_metadata(path).map_err(|_| "Cannot read hosted configuration")?;
        if !meta.is_file() || meta.len() > 1024 * 1024 {
            return Err("Hosted configuration must be a regular file under 1 MiB".into());
        }
        let input: serde_json::Value = serde_json::from_slice(
            &std::fs::read(path).map_err(|_| "Cannot read hosted configuration")?,
        )
        .map_err(|_| "Invalid hosted configuration JSON or unknown field")?;
        let config: Self = if input.get("databases").is_some() {
            serde_json::from_value::<CompactConfig>(input)
                .map_err(|_| "Invalid single-file hosted configuration or unknown field")?
                .prepare()?
        } else {
            serde_json::from_value(input)
                .map_err(|_| "Invalid hosted configuration JSON or unknown field")?
        };
        config.validate()?;
        Ok(config)
    }
    pub fn validate(&self) -> Result<(), String> {
        self.listen
            .parse::<std::net::SocketAddr>()
            .map_err(|_| "listen must be an IP address and port")?;
        if !self.config_path.is_absolute()
            || !self.state_dir.is_absolute()
            || self.workspaces.iter().any(|p| !p.is_absolute())
        {
            return Err("Hosted filesystem paths must be absolute".into());
        }
        match &self.auth {
            AuthConfig::Builtin {
                user_id,
                username,
                password_hash,
                roles,
            } => {
                if user_id.is_empty()
                    || user_id.len() > 128
                    || username.is_empty()
                    || username.len() > 128
                    || roles.iter().any(|r| r.is_empty() || r.len() > 256)
                {
                    return Err("Invalid administrator identity".into());
                }
                let hash = argon2::PasswordHash::new(password_hash)
                    .map_err(|_| "Administrator password_hash must be an Argon2id PHC hash")?;
                let params = argon2::Params::try_from(&hash)
                    .map_err(|_| "Invalid administrator Argon2 parameters")?;
                if hash.algorithm.as_str() != "argon2id"
                    || params.m_cost() < 19_456
                    || params.t_cost() < 2
                    || params.p_cost() < 1
                    || params.m_cost() > 1_048_576
                    || params.t_cost() > 20
                    || params.p_cost() > 16
                    || hash.salt.is_none()
                    || hash.hash.is_none()
                {
                    return Err("Administrator password_hash requires bounded Argon2id parameters (at least 19 MiB, 2 iterations)".into());
                }
            }
            AuthConfig::External {
                verify_url,
                allow_insecure_http,
                session_key,
                login_url,
                refresh_path,
                logout_path,
                ..
            } => {
                let url = url::Url::parse(verify_url).map_err(|_| "Invalid identity verify_url")?;
                let loopback = matches!(url.host_str(), Some("127.0.0.1" | "[::1]" | "localhost"));
                if !["http", "https"].contains(&url.scheme())
                    || url.host_str().is_none()
                    || !url.username().is_empty()
                    || url.password().is_some()
                    || url.fragment().is_some()
                    || (url.scheme() == "http" && !loopback && !allow_insecure_http)
                {
                    return Err("Identity verify_url requires HTTPS (explicit private HTTP opt-in is available)".into());
                }
                if session_key.is_empty() || session_key.len() > 200 {
                    return Err("Invalid identity session_key".into());
                }
                for path in [Some(login_url), Some(refresh_path), logout_path.as_ref()]
                    .into_iter()
                    .flatten()
                {
                    if !path.starts_with('/')
                        || path.starts_with("//")
                        || path.contains('\\')
                        || path.chars().any(|c| c.is_control() || c == ' ')
                    {
                        return Err("Browser auth paths must be same-origin absolute paths".into());
                    }
                }
            }
        }
        if self.assistant_base_url.is_some() != self.assistant_model.is_some() {
            return Err("Both assistant_base_url and assistant_model are required together".into());
        }
        Ok(())
    }
}
