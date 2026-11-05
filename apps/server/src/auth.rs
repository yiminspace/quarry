use crate::config::{AuthConfig, VerifyResponse};
use argon2::{Argon2, PasswordHash, PasswordVerifier};
use async_trait::async_trait;
use quarry_platform::{Authorizer, Principal};
use quarry_service::Failure;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::VecDeque,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

pub fn fail(status: u16, code: &str, message: &str) -> Failure {
    Failure {
        status,
        code: code.into(),
        message: message.into(),
    }
}
pub fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn hash(token: &str) -> String {
    format!("{:x}", Sha256::digest(token.as_bytes()))
}
fn token(prefix: &str) -> String {
    format!(
        "{prefix}{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}
fn permits(values: &[String], required: &str) -> bool {
    values.iter().any(|v| {
        v == required
            || v == "*"
            || v.strip_suffix(":*")
                .is_some_and(|p| required.starts_with(&format!("{p}:")))
    })
}
#[derive(Clone)]
pub struct Auth {
    config: AuthConfig,
    database: PathBuf,
    client: reqwest::Client,
    attempts: Arc<Mutex<VecDeque<Instant>>>,
}
impl Auth {
    pub fn new(config: AuthConfig, state_dir: PathBuf) -> Result<Self, Failure> {
        let database = state_dir.join("hosted-auth.sqlite3");
        if matches!(&config, AuthConfig::Builtin { .. }) {
            if std::fs::symlink_metadata(&state_dir).is_ok_and(|m| m.file_type().is_symlink())
                || std::fs::symlink_metadata(&database).is_ok_and(|m| m.file_type().is_symlink())
            {
                return Err(fail(
                    400,
                    "invalid",
                    "Authentication paths must not be symlinks",
                ));
            }
            std::fs::create_dir_all(&state_dir).map_err(|_| {
                fail(
                    503,
                    "unavailable",
                    "Cannot initialize authentication directory",
                )
            })?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&state_dir, std::fs::Permissions::from_mode(0o700))
                    .map_err(|_| {
                        fail(503, "unavailable", "Cannot secure authentication directory")
                    })?;
            }
            let db = Connection::open(&database)
                .map_err(|_| fail(503, "unavailable", "Cannot initialize authentication store"))?;
            db.execute_batch("CREATE TABLE IF NOT EXISTS sessions(access_hash TEXT PRIMARY KEY, refresh_hash TEXT UNIQUE NOT NULL, access_expires INTEGER NOT NULL, refresh_expires INTEGER NOT NULL, credential_hash TEXT NOT NULL); CREATE TABLE IF NOT EXISTS handoffs(code_hash TEXT PRIMARY KEY,parent_access_hash TEXT NOT NULL,expires INTEGER NOT NULL)").map_err(|_|fail(503,"unavailable","Cannot initialize authentication store"))?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&database, std::fs::Permissions::from_mode(0o600))
                    .map_err(|_| fail(503, "unavailable", "Cannot secure authentication store"))?;
            }
        }
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(10))
            .build()
            .map_err(|_| fail(503, "unavailable", "Cannot initialize identity client"))?;
        Ok(Self {
            config,
            database,
            client,
            attempts: Arc::new(Mutex::new(VecDeque::new())),
        })
    }
    pub fn discovery(&self) -> Value {
        match &self.config {
            AuthConfig::Builtin { .. } => {
                json!({"apiBase":"/api/db","auth":{"mode":"builtin","sessionKey":"quarry.hosted.session.v1","loginUrl":"/quarry/","refreshPath":"/api/hosted/auth/refresh","logoutPath":"/api/hosted/auth/logout"}})
            }
            AuthConfig::External {
                session_key,
                login_url,
                refresh_path,
                logout_path,
                ..
            } => {
                json!({"apiBase":"/api/db","auth":{"mode":"external","sessionKey":session_key,"loginUrl":login_url,"refreshPath":refresh_path,"logoutPath":logout_path}})
            }
        }
    }
    fn db(&self) -> Result<Connection, Failure> {
        let db = Connection::open(&self.database)
            .map_err(|_| fail(503, "unavailable", "Authentication store unavailable"))?;
        db.busy_timeout(Duration::from_secs(5))
            .map_err(|_| fail(503, "unavailable", "Authentication store unavailable"))?;
        Ok(db)
    }
    fn builtin(&self) -> Result<(&str, &str, &str, &[String]), Failure> {
        match &self.config {
            AuthConfig::Builtin {
                user_id,
                username,
                password_hash,
                roles,
            } => Ok((user_id, username, password_hash, roles)),
            _ => Err(fail(404, "not_found", "Use the configured platform login")),
        }
    }
    fn fingerprint(&self) -> Result<String, Failure> {
        let (id, username, password_hash, _) = self.builtin()?;
        Ok(hash(
            &serde_json::json!([id, username, password_hash]).to_string(),
        ))
    }
    fn principal(&self) -> Result<Principal, Failure> {
        let (id, _, _, roles) = self.builtin()?;
        Ok(Principal {
            id: id.into(),
            permissions: vec![
                "lab:db:read".into(),
                "lab:db:local".into(),
                "lab:db:schema:export".into(),
                "lab:ai:invoke".into(),
            ],
            roles: roles.to_vec(),
        })
    }
    fn session(&self, access: String, refresh: String) -> Result<Value, Failure> {
        let p = self.principal()?;
        Ok(
            json!({"access_token":access,"refresh_token":refresh,"expires_in":900,"token_type":"Bearer","user":{"id":p.id,"username":self.builtin()?.1}}),
        )
    }
    pub async fn login(&self, username: String, password: String) -> Result<Value, Failure> {
        let (_, expected, password_hash, _) = self.builtin()?;
        if username.len() > 128 || password.len() > 4096 {
            return Err(fail(401, "unauthorized", "Invalid username or password"));
        }
        {
            let mut attempts = self
                .attempts
                .lock()
                .map_err(|_| fail(503, "unavailable", "Authentication temporarily unavailable"))?;
            while attempts
                .front()
                .is_some_and(|t| t.elapsed() > Duration::from_secs(60))
            {
                attempts.pop_front();
            }
            if attempts.len() >= 20 {
                return Err(fail(
                    429,
                    "rate_limited",
                    "Too many login attempts; retry later",
                ));
            }
            attempts.push_back(Instant::now());
        }
        let expected = expected.to_owned();
        let encoded = password_hash.to_owned();
        let fingerprint = self.fingerprint()?;
        let valid = tokio::task::spawn_blocking(move || {
            PasswordHash::new(&encoded).is_ok_and(|h| {
                Argon2::default()
                    .verify_password(password.as_bytes(), &h)
                    .is_ok()
            }) && username == expected
        })
        .await
        .unwrap_or(false);
        if !valid {
            return Err(fail(401, "unauthorized", "Invalid username or password"));
        }
        let access = token("qa_");
        let refresh = token("qr_");
        let db = self.db()?;
        db.execute("DELETE FROM sessions WHERE refresh_expires<=?", [now()])
            .map_err(|_| fail(503, "unavailable", "Authentication store unavailable"))?;
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?)",
            params![
                hash(&access),
                hash(&refresh),
                now() + 900,
                now() + 30 * 86400,
                fingerprint
            ],
        )
        .map_err(|_| fail(503, "unavailable", "Cannot save session"))?;
        self.session(access, refresh)
    }
    pub fn refresh(&self, refresh: &str) -> Result<Value, Failure> {
        let fingerprint = self.fingerprint()?;
        if refresh.len() > 200 {
            return Err(fail(401, "unauthorized", "Session expired"));
        }
        let access = token("qa_");
        let next = token("qr_");
        let changed=self.db()?.execute("UPDATE sessions SET access_hash=?,refresh_hash=?,access_expires=? WHERE refresh_hash=? AND refresh_expires>? AND credential_hash=?",params![hash(&access),hash(&next),now()+900,hash(refresh),now(),fingerprint]).map_err(|_|fail(503,"unavailable","Cannot refresh session"))?;
        if changed != 1 {
            return Err(fail(401, "unauthorized", "Session expired"));
        }
        self.session(access, next)
    }
    pub async fn handoff(&self, bearer: &str) -> Result<Value, Failure> {
        self.builtin()?;
        self.authorize(bearer, "lab:db:read").await?;
        let code = token("qh_");
        let db = self.db()?;
        db.execute("DELETE FROM handoffs WHERE expires<=?", [now()])
            .map_err(|_| fail(503, "unavailable", "Authentication store unavailable"))?;
        db.execute(
            "INSERT INTO handoffs VALUES(?,?,?)",
            params![hash(&code), hash(bearer), now() + 60],
        )
        .map_err(|_| fail(503, "unavailable", "Cannot create browser handoff"))?;
        Ok(json!({"code":code,"expires_in":60}))
    }
    pub fn consume_handoff(&self, code: &str) -> Result<Value, Failure> {
        let fingerprint = self.fingerprint()?;
        if code.len() > 200 {
            return Err(fail(401, "unauthorized", "Browser handoff expired"));
        }
        let mut db = self.db()?;
        let tx = db
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(|_| fail(503, "unavailable", "Authentication store unavailable"))?;
        let valid=tx.query_row("SELECT 1 FROM handoffs h JOIN sessions s ON h.parent_access_hash=s.access_hash WHERE h.code_hash=? AND h.expires>? AND s.access_expires>? AND s.refresh_expires>? AND s.credential_hash=?",params![hash(code),now(),now(),now(),fingerprint],|_|Ok(true)).optional().map_err(|_|fail(503,"unavailable","Authentication store unavailable"))?.unwrap_or(false);
        if !valid {
            return Err(fail(401, "unauthorized", "Browser handoff expired"));
        }
        tx.execute("DELETE FROM handoffs WHERE code_hash=?", [hash(code)])
            .map_err(|_| fail(503, "unavailable", "Cannot consume browser handoff"))?;
        let access = token("qa_");
        let refresh = token("qr_");
        tx.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?)",
            params![
                hash(&access),
                hash(&refresh),
                now() + 900,
                now() + 30 * 86400,
                fingerprint
            ],
        )
        .map_err(|_| fail(503, "unavailable", "Cannot save browser session"))?;
        tx.commit()
            .map_err(|_| fail(503, "unavailable", "Cannot save browser session"))?;
        self.session(access, refresh)
    }
    pub fn logout(&self, refresh: &str) -> Result<Value, Failure> {
        self.builtin()?;
        if refresh.len() > 200 {
            return Err(fail(400, "invalid", "Invalid refresh token"));
        }
        self.db()?
            .execute("DELETE FROM sessions WHERE refresh_hash=?", [hash(refresh)])
            .map_err(|_| fail(503, "unavailable", "Cannot revoke session"))?;
        Ok(json!({"revoked":true}))
    }
}
#[async_trait]
impl Authorizer for Auth {
    async fn authorize(&self, bearer: &str, permission: &str) -> Result<Principal, Failure> {
        if bearer.is_empty() || bearer.len() > 8192 || bearer.chars().any(char::is_whitespace) {
            return Err(fail(
                401,
                "unauthorized",
                "A valid bearer credential is required",
            ));
        }
        match &self.config {
            AuthConfig::Builtin { .. } => {
                let valid:bool=self.db()?.query_row("SELECT 1 FROM sessions WHERE access_hash=? AND access_expires>? AND credential_hash=?",params![hash(bearer),now(),self.fingerprint()?],|_|Ok(true)).optional().map_err(|_|fail(503,"unavailable","Authentication store unavailable"))?.unwrap_or(false);
                if !valid {
                    return Err(fail(401, "unauthorized", "Session expired"));
                }
                let p = self.principal()?;
                if !permits(&p.permissions, permission) {
                    return Err(fail(403, "forbidden", "Permission denied"));
                }
                Ok(p)
            }
            AuthConfig::External {
                verify_url,
                response,
                ..
            } => {
                let response_http = self
                    .client
                    .get(verify_url)
                    .bearer_auth(bearer)
                    .header("x-required-permission", permission)
                    .header("x-required-scope", permission)
                    .send()
                    .await
                    .map_err(|_| {
                        fail(503, "identity_unavailable", "Identity service unavailable")
                    })?;
                let status = response_http.status().as_u16();
                if status != 200 {
                    return Err(fail(
                        if [401, 403].contains(&status) {
                            status
                        } else {
                            503
                        },
                        "identity_unavailable",
                        "Identity authorization failed",
                    ));
                }
                let p = match response {
                    VerifyResponse::Headers => {
                        let field = |key: &str| -> Result<String, Failure> {
                            let text = response_http
                                .headers()
                                .get(key)
                                .and_then(|h| h.to_str().ok())
                                .unwrap_or("");
                            if text.len() > 65536 {
                                return Err(fail(
                                    503,
                                    "identity_unavailable",
                                    "Invalid identity response",
                                ));
                            }
                            Ok(text.into())
                        };
                        let list = |key: &str| -> Result<Vec<String>, Failure> {
                            let text = field(key)?;
                            let values = text
                                .split(',')
                                .map(str::trim)
                                .filter(|s| !s.is_empty())
                                .map(String::from)
                                .collect::<Vec<_>>();
                            if values.len() > 1000 || values.iter().any(|s| s.len() > 256) {
                                return Err(fail(
                                    503,
                                    "identity_unavailable",
                                    "Invalid identity response",
                                ));
                            }
                            Ok(values)
                        };
                        Principal {
                            id: field("x-user-id")?,
                            permissions: list("x-user-permissions")?,
                            roles: list("x-user-roles")?,
                        }
                    }
                    VerifyResponse::Json => {
                        let mut response_http = response_http;
                        let mut bytes = Vec::new();
                        while let Some(chunk) = response_http.chunk().await.map_err(|_| {
                            fail(503, "identity_unavailable", "Identity response unavailable")
                        })? {
                            if bytes.len() + chunk.len() > 1024 * 1024 {
                                return Err(fail(
                                    503,
                                    "identity_unavailable",
                                    "Identity response too large",
                                ));
                            }
                            bytes.extend_from_slice(&chunk);
                        }
                        serde_json::from_slice::<Principal>(&bytes).map_err(|_| {
                            fail(503, "identity_unavailable", "Invalid identity response")
                        })?
                    }
                };
                if p.id.is_empty()
                    || p.id.len() > 128
                    || p.permissions.len() > 1000
                    || p.roles.len() > 1000
                    || p.permissions
                        .iter()
                        .chain(p.roles.iter())
                        .any(|s| s.len() > 256)
                {
                    return Err(fail(
                        503,
                        "identity_unavailable",
                        "Invalid identity principal",
                    ));
                }
                if !permits(&p.permissions, permission) {
                    return Err(fail(403, "forbidden", "Permission denied"));
                }
                Ok(p)
            }
        }
    }
}
