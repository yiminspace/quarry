use crate::auth::fail;
use quarry_service::{Failure, Result};
use rand_core::{OsRng, RngCore};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    sync::Mutex,
    time::{Duration, Instant},
};

const TICKET_TTL: Duration = Duration::from_secs(60);
pub const SESSION_TTL: Duration = Duration::from_secs(30 * 60);
const CAPACITY: usize = 1024;
pub const COOKIE: &str = "__Secure-quarry-cli";

#[derive(Clone)]
pub struct Access {
    pub bearer: String,
    pub resource: String,
}

#[derive(Default)]
struct Entries {
    tickets: HashMap<[u8; 32], (Instant, Access)>,
    sessions: HashMap<String, (Instant, [u8; 32], Access)>,
}

#[derive(Default)]
pub struct Handoffs(Mutex<Entries>);

fn hash(value: &str) -> [u8; 32] {
    Sha256::digest(value.as_bytes()).into()
}

fn secret() -> String {
    let mut bytes = [0u8; 32];
    OsRng.fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn unavailable() -> Failure {
    fail(503, "unavailable", "Browser session service unavailable")
}

fn expired() -> Failure {
    fail(401, "unauthorized", "Open the workbench again with bcli.")
}

impl Handoffs {
    pub fn issue(&self, access: Access) -> Result<String> {
        let mut entries = self.0.lock().map_err(|_| unavailable())?;
        let now = Instant::now();
        entries.tickets.retain(|_, (until, _)| *until > now);
        if entries.tickets.len() >= CAPACITY {
            return Err(fail(429, "busy", "Too many pending browser launches"));
        }
        let code = secret();
        entries
            .tickets
            .insert(hash(&code), (now + TICKET_TTL, access));
        Ok(code)
    }

    pub fn consume(&self, code: &str) -> Result<(String, String)> {
        let mut entries = self.0.lock().map_err(|_| unavailable())?;
        let now = Instant::now();
        entries.sessions.retain(|_, (until, _, _)| *until > now);
        if entries.sessions.len() >= CAPACITY {
            return Err(fail(429, "busy", "Too many browser sessions"));
        }
        let (_, access) = entries
            .tickets
            .remove(&hash(code))
            .filter(|(until, _)| *until > now)
            .ok_or_else(expired)?;
        let id = uuid::Uuid::new_v4().simple().to_string();
        let cookie = secret();
        entries
            .sessions
            .insert(id.clone(), (now + SESSION_TTL, hash(&cookie), access));
        Ok((id, cookie))
    }

    pub fn session(&self, id: &str, cookie: &str) -> Result<(Access, Duration)> {
        let mut entries = self.0.lock().map_err(|_| unavailable())?;
        let now = Instant::now();
        entries.sessions.retain(|_, (until, _, _)| *until > now);
        let (until, expected, access) = entries.sessions.get(id).ok_or_else(expired)?;
        if expected != &hash(cookie) {
            return Err(expired());
        }
        Ok((access.clone(), until.saturating_duration_since(now)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expired_tickets_and_sessions_fail_closed() {
        let sessions = Handoffs::default();
        let access = Access {
            bearer: "fixture".into(),
            resource: "one".into(),
        };
        let code = sessions.issue(access.clone()).unwrap();
        sessions
            .0
            .lock()
            .unwrap()
            .tickets
            .get_mut(&hash(&code))
            .unwrap()
            .0 = Instant::now();
        assert!(sessions.consume(&code).is_err());
        let code = sessions.issue(access).unwrap();
        let (id, cookie) = sessions.consume(&code).unwrap();
        assert_eq!(sessions.session(&id, &cookie).unwrap().0.resource, "one");
        assert!(sessions.session(&id, "other").is_err());
        sessions.0.lock().unwrap().sessions.get_mut(&id).unwrap().0 = Instant::now();
        assert!(sessions.session(&id, &cookie).is_err());
    }
}
