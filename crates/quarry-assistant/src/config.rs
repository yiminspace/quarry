//! Explicit, local model configuration. Secrets never appear in status or Debug output.
use crate::{AssistantError, Result};
use fs2::FileExt;
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use toml_edit::{DocumentMut, value};
use url::Url;
use uuid::Uuid;

#[derive(Clone)]
pub struct ModelConfig {
    pub base_url: String,
    pub model: String,
    pub api_key: String,
    /// Trusted local argv, never populated by model messages or HTTP configuration.
    /// Resolve immediately before each model request; do not persist its output.
    pub credential_command: Option<Vec<String>>,
    pub bifrost: bool,
    pub thinking: String,
    pub timeout: Duration,
}
impl std::fmt::Debug for ModelConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ModelConfig")
            .field("model", &self.model)
            .field("api_key", &"[REDACTED]")
            .field("bifrost", &self.bifrost)
            .field("thinking", &self.thinking)
            .field("timeout", &self.timeout)
            .finish_non_exhaustive()
    }
}
impl ModelConfig {
    pub fn validate(&self) -> Result<()> {
        validate_url(&self.base_url)?;
        if self.model.trim().is_empty()
            || self.model.chars().count() > 200
            || self.model.chars().any(char::is_control)
        {
            return Err(AssistantError::invalid(
                "A model name is required (maximum 200 characters).",
            ));
        }
        if !["default", "disabled"].contains(&self.thinking.as_str())
            || !(1..=300).contains(&self.timeout.as_secs())
        {
            return Err(AssistantError::invalid(
                "Invalid assistant timeout or thinking mode.",
            ));
        }
        if self.api_key.chars().any(char::is_control) || self.api_key.len() > 8192 {
            return Err(AssistantError::invalid(
                "API key must be a nonempty single line.",
            ));
        }
        if let Some(argv) = &self.credential_command {
            if !self.api_key.is_empty() {
                return Err(AssistantError::invalid(
                    "Choose one model credential source.",
                ));
            }
            validate_command(argv)?;
        }
        Ok(())
    }
}

fn validate_command(argv: &[String]) -> Result<()> {
    if argv.is_empty()
        || argv.len() > 32
        || !Path::new(&argv[0]).is_absolute()
        || argv
            .iter()
            .any(|arg| arg.len() > 4096 || arg.chars().any(char::is_control))
        || argv.iter().map(String::len).sum::<usize>() > 16_384
    {
        return Err(AssistantError::invalid(
            "Credential command requires an absolute executable and bounded, single-line arguments.",
        ));
    }
    Ok(())
}
#[derive(Clone)]
pub enum Credential {
    Environment(String),
    Stored(String),
    None,
}
#[derive(Clone)]
pub struct ConfigManager {
    pub config_path: PathBuf,
    pub credentials_path: PathBuf,
    environment: HashMap<String, String>,
    system_language: String,
    system_language_provider: Option<Arc<dyn Fn() -> String + Send + Sync>>,
}

impl std::fmt::Debug for Credential {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Environment(name) => f.debug_tuple("Environment").field(name).finish(),
            Self::Stored(_) => f.write_str("Stored([REDACTED])"),
            Self::None => f.write_str("None"),
        }
    }
}
impl std::fmt::Debug for ConfigManager {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ConfigManager")
            .field("config_path", &self.config_path)
            .field("credentials_path", &self.credentials_path)
            .finish_non_exhaustive()
    }
}
pub fn resolve_language(value: &str) -> &'static str {
    let first = value.split(['-', '_', '.', '@']).next().unwrap_or("");
    if first.eq_ignore_ascii_case("zh") || first.eq_ignore_ascii_case("chinese") {
        "zh"
    } else {
        "en"
    }
}
fn validate_url(raw: &str) -> Result<String> {
    let u = Url::parse(raw).map_err(|_| AssistantError::invalid("Invalid model base URL."))?;
    if raw.len() > 2048
        || raw.chars().any(char::is_whitespace)
        || !["http", "https"].contains(&u.scheme())
        || u.host_str().is_none()
        || !u.username().is_empty()
        || u.password().is_some()
        || u.query().is_some()
        || u.fragment().is_some()
        || u.port() == Some(0)
    {
        return Err(AssistantError::invalid(
            "Model base URL must be HTTP(S), without credentials, query or fragment.",
        ));
    }
    Ok(raw.trim_end_matches('/').to_owned())
}
static MISSING: toml_edit::Item = toml_edit::Item::None;
fn at<'a>(d: &'a DocumentMut, path: &[&str]) -> &'a toml_edit::Item {
    let Some(mut item) = d.get(path[0]) else {
        return &MISSING;
    };
    for key in &path[1..] {
        let Some(next) = item.get(*key) else {
            return &MISSING;
        };
        item = next;
    }
    item
}
fn config_error(_: impl std::fmt::Display) -> AssistantError {
    AssistantError::invalid("Cannot read Quarry configuration. Check config.toml.")
}
fn atomic(path: &Path, text: &str, private: bool) -> Result<()> {
    let parent = path.parent().ok_or_else(|| {
        AssistantError::invalid("Configuration requires an explicit parent directory")
    })?;
    fs::create_dir_all(parent).map_err(config_error)?;
    if path.is_symlink() {
        return Err(AssistantError::invalid(
            "Refusing to replace a symbolic-link configuration file",
        ));
    }
    let temporary = parent.join(format!(".quarry-{}", Uuid::new_v4()));
    let mut options = fs::OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
        let mode = if private {
            0o600
        } else {
            fs::metadata(path)
                .ok()
                .map(|m| m.permissions().mode() & 0o777)
                .unwrap_or(0o600)
        };
        options.mode(mode);
    }
    let result = (|| {
        let mut f = options.open(&temporary).map_err(config_error)?;
        f.write_all(text.as_bytes()).map_err(config_error)?;
        f.sync_all().map_err(config_error)?;
        fs::rename(&temporary, path).map_err(config_error)?;
        Ok(())
    })();
    if temporary.exists() {
        let _ = fs::remove_file(temporary);
    }
    result
}
impl ConfigManager {
    pub fn new(
        config_path: impl Into<PathBuf>,
        environment: HashMap<String, String>,
        system_language: impl AsRef<str>,
    ) -> Self {
        let config_path = config_path.into();
        let credentials_path = config_path.with_file_name("assistant-credentials.json");
        Self {
            config_path,
            credentials_path,
            environment,
            system_language: resolve_language(system_language.as_ref()).into(),
            system_language_provider: None,
        }
    }
    /// Opt into a live language source at the embedding boundary. The default
    /// constructor remains an explicit snapshot and never reads OS settings.
    pub fn with_system_language_provider(
        mut self,
        provider: impl Fn() -> String + Send + Sync + 'static,
    ) -> Self {
        self.system_language_provider = Some(Arc::new(provider));
        self
    }
    fn document(&self) -> Result<DocumentMut> {
        if !self.config_path.exists() {
            return Ok(DocumentMut::new());
        }
        fs::read_to_string(&self.config_path)
            .map_err(config_error)?
            .parse()
            .map_err(config_error)
    }
    pub fn load(&self) -> Result<ModelConfig> {
        let d = self.document()?;
        let base_url = at(&d, &["assistant", "base_url"]).as_str().ok_or_else(|| {
            AssistantError::invalid(
                "Model not configured. Run `qy ai configure --help` in your terminal.",
            )
        })?;
        let model = at(&d, &["assistant", "model"]).as_str().ok_or_else(|| {
            AssistantError::invalid(
                "Model not configured. Run `qy ai configure --help` in your terminal.",
            )
        })?;
        let auth = at(&d, &["assistant", "auth"]).as_str().unwrap_or("env");
        let credential_command = if auth == "command" {
            let array = at(&d, &["assistant", "credential_command"])
                .as_array()
                .ok_or_else(|| {
                    AssistantError::invalid("Credential command must be an argv array.")
                })?;
            let argv = array
                .iter()
                .map(|item| {
                    item.as_str().map(str::to_owned).ok_or_else(|| {
                        AssistantError::invalid(
                            "Credential command must contain only string arguments.",
                        )
                    })
                })
                .collect::<Result<Vec<_>>>()?;
            validate_command(&argv)?;
            Some(argv)
        } else {
            None
        };
        let api_key = match auth {
            "none" | "command" => String::new(),
            "env" => self
                .environment
                .get(
                    at(&d, &["assistant", "api_key_env"])
                        .as_str()
                        .unwrap_or("QUARRY_AI_API_KEY"),
                )
                .cloned()
                .unwrap_or_default(),
            "stored" => {
                let raw = fs::read_to_string(&self.credentials_path).unwrap_or_default();
                let saved: Value = serde_json::from_str(&raw).unwrap_or(Value::Null);
                if saved["base_url"] == base_url {
                    saved["api_key"].as_str().unwrap_or("").to_owned()
                } else {
                    String::new()
                }
            }
            _ => {
                return Err(AssistantError::invalid(
                    "Invalid assistant authentication mode.",
                ));
            }
        };
        if !["none", "command"].contains(&auth)
            && (api_key.is_empty() || api_key.chars().any(char::is_control))
        {
            return Err(AssistantError::invalid(
                "Model API key is unavailable. Run `qy ai configure --help` in your terminal.",
            ));
        }
        let timeout = at(&d, &["assistant", "timeout"]).as_integer().unwrap_or(30);
        if !(1..=300).contains(&timeout) {
            return Err(AssistantError::invalid(
                "Invalid assistant timeout or thinking mode.",
            ));
        }
        let config = ModelConfig {
            base_url: validate_url(base_url)?,
            model: model.to_owned(),
            api_key,
            credential_command,
            bifrost: at(&d, &["assistant", "bifrost"]).as_bool().unwrap_or(false),
            thinking: at(&d, &["assistant", "thinking"])
                .as_str()
                .unwrap_or("default")
                .to_owned(),
            timeout: Duration::from_secs(timeout as u64),
        };
        config.validate()?;
        Ok(config)
    }
    pub fn status(&self) -> Value {
        match self.load() {
            Ok(c) => json!({"configured":true,"model":c.model}),
            Err(_) => json!({"configured":false}),
        }
    }
    pub fn details(&self) -> Result<Value> {
        let d = self.document()?;
        let mut out = self.status();
        for key in [
            "base_url",
            "model",
            "auth",
            "api_key_env",
            "bifrost",
            "thinking",
            "timeout",
        ] {
            let item = at(&d, &["assistant", key]);
            if let Some(s) = item.as_str() {
                out[key] = json!(s);
            } else if let Some(b) = item.as_bool() {
                out[key] = json!(b);
            } else if let Some(i) = item.as_integer() {
                out[key] = json!(i);
            }
        }
        out["api_key"] = if out["configured"] == true
            && at(&d, &["assistant", "auth"]).as_str().unwrap_or("env") != "none"
        {
            json!("********")
        } else {
            Value::Null
        };
        Ok(out)
    }
    fn with_document<T>(&self, f: impl FnOnce(&mut DocumentMut) -> Result<T>) -> Result<T> {
        let parent = self
            .config_path
            .parent()
            .ok_or_else(|| AssistantError::invalid("Configuration requires a parent directory"))?;
        fs::create_dir_all(parent).map_err(config_error)?;
        let lock = fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(self.config_path.with_extension("toml.lock"))
            .map_err(config_error)?;
        lock.lock_exclusive().map_err(config_error)?;
        let mut document = self.document()?;
        let result = f(&mut document)?;
        atomic(&self.config_path, &document.to_string(), false)?;
        Ok(result)
    }
    pub fn configure(
        &self,
        base_url: &str,
        model: &str,
        credential: Credential,
        bifrost: bool,
        thinking: &str,
        timeout: u64,
    ) -> Result<Value> {
        let base_url = validate_url(base_url)?;
        let probe = ModelConfig {
            base_url: base_url.clone(),
            model: model.trim().to_owned(),
            api_key: match &credential {
                Credential::Stored(key) => key.clone(),
                _ => String::new(),
            },
            credential_command: None,
            bifrost,
            thinking: thinking.to_owned(),
            timeout: Duration::from_secs(timeout),
        };
        probe.validate()?;
        if let Credential::Environment(name) = &credential {
            let mut chars = name.chars();
            if !chars
                .next()
                .is_some_and(|c| c == '_' || c.is_ascii_alphabetic())
                || !chars.all(|c| c == '_' || c.is_ascii_alphanumeric())
            {
                return Err(AssistantError::invalid(
                    "Invalid API key environment variable name.",
                ));
            }
        }
        if let Credential::Stored(key) = &credential
            && key.trim().is_empty()
        {
            return Err(AssistantError::invalid(
                "API key must be a nonempty single line.",
            ));
        }
        self.with_document(|d| {
            if d.get("assistant").is_some_and(|i| !i.is_table()) {
                return Err(AssistantError::invalid(
                    "Invalid [assistant] configuration.",
                ));
            }
            let mut table = toml_edit::Table::new();
            table["base_url"] = value(&base_url);
            table["model"] = value(&probe.model);
            table["bifrost"] = value(bifrost);
            table["thinking"] = value(thinking);
            table["timeout"] = value(timeout as i64);
            match &credential {
                Credential::None => table["auth"] = value("none"),
                Credential::Environment(name) => {
                    table["auth"] = value("env");
                    table["api_key_env"] = value(name);
                }
                Credential::Stored(key) => {
                    table["auth"] = value("stored");
                    atomic(
                        &self.credentials_path,
                        &json!({"base_url":base_url,"api_key":key}).to_string(),
                        true,
                    )?;
                }
            }
            d["assistant"] = toml_edit::Item::Table(table);
            Ok(())
        })?;
        if !matches!(credential, Credential::Stored(_)) && self.credentials_path.exists() {
            fs::remove_file(&self.credentials_path).map_err(config_error)?;
        }
        self.details()
    }
    pub fn preferences(&self) -> Result<Value> {
        let d = self.document()?;
        let item = at(&d, &["gui", "assistant", "reply_language"]);
        let language = item.as_str().unwrap_or("system");
        if !["zh", "en", "system", "auto"].contains(&language) {
            return Err(AssistantError::invalid(
                "Invalid GUI reply language. Use `qy ai language zh|en|system`.",
            ));
        }
        let language = if language == "auto" {
            "system"
        } else {
            language
        };
        let resolved = if language == "system" {
            self.system_language_provider
                .as_ref()
                .map(|provider| resolve_language(&provider()))
                .unwrap_or(self.system_language.as_str())
        } else {
            language
        };
        Ok(
            json!({"replyLanguage":language,"configured":!item.is_none(),"resolvedReplyLanguage":resolved}),
        )
    }
    pub fn set_reply_language(&self, language: &str) -> Result<Value> {
        if !["zh", "en", "system", "auto"].contains(&language) {
            return Err(AssistantError::invalid(
                "Reply language must be zh, en, or system.",
            ));
        }
        let language = if language == "auto" {
            "system"
        } else {
            language
        };
        self.with_document(|d| {
            d["gui"]["assistant"]["reply_language"] = value(language);
            Ok(())
        })?;
        self.preferences()
    }
}
