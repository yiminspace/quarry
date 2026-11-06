//! Explicit discovery of PostgreSQL routes that reached the same database instance.
use crate::{
    federated_cli::Target,
    federation::{Catalog, Federation, LocalTarget, Resource, Status},
};
use futures_util::{StreamExt, stream};
use quarry_core::{Engine, QueryRequest};
use quarry_runtime::ConnectionRecord;
use quarry_service::QuarryService;
use serde::Serialize;
use serde_json::Value;
use std::{collections::BTreeMap, net::IpAddr, time::Duration};

const MAX_TARGETS: usize = 32;
const CONCURRENCY: usize = 4;
const PROBE_TIMEOUT: Duration = Duration::from_secs(8);
const IDENTITY_SQL: &str = "\
SELECT pg_catalog.current_database() AS database_name,
       (SELECT oid::text FROM pg_catalog.pg_database
          WHERE datname = pg_catalog.current_database()) AS database_oid,
       CASE WHEN pg_catalog.has_function_privilege('pg_catalog.pg_control_system()', 'EXECUTE')
            THEN (SELECT system_identifier::text FROM pg_catalog.pg_control_system())
       END AS system_id,
       pg_catalog.inet_server_addr()::text AS server_address,
       pg_catalog.inet_server_port() AS server_port,
       extract(epoch FROM pg_catalog.pg_postmaster_start_time())::text AS instance_start";

#[derive(Serialize)]
pub struct Discovery {
    pub candidates: Vec<VerifiedCandidate>,
    pub skipped: Vec<VerificationSkip>,
    pub unavailable_local_count: usize,
}

#[derive(Serialize)]
pub struct VerifiedCandidate {
    pub resource_id: String,
    pub local: LocalTarget,
    pub local_db: String,
    pub cloud_db: String,
    pub already_linked: bool,
}

#[derive(Serialize)]
pub struct VerificationSkip {
    pub resource_id: String,
    pub reason: SkipReason,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SkipReason {
    UnsupportedEngine,
    CloudUnavailable,
    NoCompatibleLocal,
    TargetLimit,
    CloudIdentityUnavailable,
    LocalIdentityUnavailable,
    NoExactMatch,
    Ambiguous,
    ConflictingAssociation,
}

#[derive(PartialEq, Eq)]
struct PostgresIdentity {
    database_name: String,
    database_oid: u32,
    system_id: i64,
    server_address: IpAddr,
    server_port: u16,
    instance_start: (u64, u32),
}

impl PostgresIdentity {
    fn from_rows(rows: &[Value]) -> Option<Self> {
        let [row] = rows else { return None };
        let database_name = row["database_name"].as_str()?.to_owned();
        let database_oid = row["database_oid"].as_str()?.parse().ok()?;
        let system_id = row["system_id"].as_str()?.parse().ok()?;
        let server_address = parse_address(row["server_address"].as_str()?)?;
        let server_port = u16::try_from(row["server_port"].as_u64()?).ok()?;
        let instance_start = parse_epoch(row["instance_start"].as_str()?)?;
        if database_name.is_empty() || database_oid == 0 || system_id == 0 || server_port == 0 {
            return None;
        }
        Some(Self {
            database_name,
            database_oid,
            system_id,
            server_address,
            server_port,
            instance_start,
        })
    }
}

fn parse_address(text: &str) -> Option<IpAddr> {
    let Some((address, mask)) = text.split_once('/') else {
        return text.parse().ok();
    };
    let address: IpAddr = address.parse().ok()?;
    let full_mask = if address.is_ipv4() { "32" } else { "128" };
    (mask == full_mask).then_some(address)
}

fn parse_epoch(text: &str) -> Option<(u64, u32)> {
    let (seconds, fraction) = text.split_once('.').unwrap_or((text, ""));
    if text.ends_with('.')
        || !seconds.bytes().all(|byte| byte.is_ascii_digit())
        || fraction.len() > 6
        || !fraction.bytes().all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    let seconds = seconds.parse().ok().filter(|seconds| *seconds != 0)?;
    let micros = format!("{fraction:0<6}").parse().ok()?;
    Some((seconds, micros))
}

fn project_name(record: &ConnectionRecord) -> Option<&str> {
    record
        .group
        .as_deref()
        .filter(|group| !group.is_empty())
        .or_else(|| record.workspace.file_name()?.to_str())
}

fn match_identity<'a>(
    cloud: Option<&PostgresIdentity>,
    locals: impl IntoIterator<Item = (&'a LocalTarget, Option<&'a PostgresIdentity>)>,
    existing: Option<&LocalTarget>,
) -> Result<(&'a LocalTarget, bool), SkipReason> {
    let cloud = cloud.ok_or(SkipReason::CloudIdentityUnavailable)?;
    let mut exact = None;
    let mut unavailable = false;
    for (local, identity) in locals {
        let Some(identity) = identity else {
            unavailable = true;
            continue;
        };
        if identity == cloud && exact.replace(local).is_some() {
            return Err(SkipReason::Ambiguous);
        }
    }
    let local = exact.ok_or(if unavailable {
        SkipReason::LocalIdentityUnavailable
    } else {
        SkipReason::NoExactMatch
    })?;
    if existing.is_some_and(|linked| linked != local) {
        return Err(SkipReason::ConflictingAssociation);
    }
    Ok((local, existing.is_some()))
}

struct LocalProbe<'a> {
    target: LocalTarget,
    record: &'a ConnectionRecord,
}

enum Probe<'a> {
    Local(&'a ConnectionRecord),
    Cloud(&'a Resource),
}

impl Probe<'_> {
    async fn identity(
        self,
        host: &QuarryService,
        federation: &Federation,
    ) -> Option<PostgresIdentity> {
        tokio::time::timeout(PROBE_TIMEOUT, async {
            match self {
                Self::Local(record) => {
                    let mut request = QueryRequest::new(IDENTITY_SQL);
                    request.policy.timeout_ms = Some(PROBE_TIMEOUT.as_millis() as u64);
                    request.policy.max_rows = Some(1);
                    let result = host.execute(record, request).await.ok()?;
                    if result.truncated {
                        return None;
                    }
                    PostgresIdentity::from_rows(&result.rows_json())
                }
                Self::Cloud(resource) => {
                    let request_id = uuid::Uuid::new_v4().to_string();
                    let result = Target::Cloud(resource.clone())
                        .execute(host, Some(federation), IDENTITY_SQL, 1, 0, &request_id)
                        .await
                        .ok()?;
                    if result["truncated"].as_bool() != Some(false)
                        || result["resourceId"].as_str() != Some(resource.upstream_id.as_str())
                        || result["requestId"].as_str() != Some(request_id.as_str())
                    {
                        return None;
                    }
                    PostgresIdentity::from_rows(result["rows"].as_array()?)
                }
            }
        })
        .await
        .ok()?
    }
}

pub async fn discover(
    host: &QuarryService,
    federation: &Federation,
    catalog: &Catalog,
    locals: &[ConnectionRecord],
    project: Option<&str>,
) -> Discovery {
    let mut local_projects = BTreeMap::<String, BTreeMap<String, LocalProbe<'_>>>::new();
    for record in locals {
        let Some(name) = project_name(record) else {
            continue;
        };
        if project.is_some_and(|filter| !name.eq_ignore_ascii_case(filter))
            || !record
                .spec()
                .is_ok_and(|spec| spec.engine == Engine::Postgres)
        {
            continue;
        }
        let target = LocalTarget {
            id: record.target_id(),
            workspace: record.workspace.clone(),
            key: record.key.clone(),
        };
        local_projects
            .entry(name.to_ascii_lowercase())
            .or_default()
            .insert(target.id.clone(), LocalProbe { target, record });
    }

    let mut discovery = Discovery {
        candidates: Vec::new(),
        skipped: Vec::new(),
        unavailable_local_count: 0,
    };
    let mut probes = BTreeMap::new();
    let mut planned = Vec::new();
    for resource in &catalog.resources {
        if project.is_some_and(|filter| !resource.project.eq_ignore_ascii_case(filter)) {
            continue;
        }
        let compatible = local_projects.get(&resource.project.to_ascii_lowercase());
        let result = if resource.engine != Engine::Postgres {
            Err(SkipReason::UnsupportedEngine)
        } else if resource.status != Status::Connected {
            Err(SkipReason::CloudUnavailable)
        } else if let Some(compatible) = compatible {
            let additional = compatible
                .keys()
                .filter(|id| !probes.contains_key(*id))
                .count()
                + 1;
            if compatible.len() >= MAX_TARGETS || probes.len() + additional > MAX_TARGETS {
                Err(SkipReason::TargetLimit)
            } else {
                for (id, local) in compatible {
                    probes
                        .entry(id.clone())
                        .or_insert(Probe::Local(local.record));
                }
                probes.insert(resource.id.clone(), Probe::Cloud(resource));
                Ok(compatible)
            }
        } else {
            Err(SkipReason::NoCompatibleLocal)
        };
        match result {
            Ok(compatible) => planned.push((resource, compatible)),
            Err(reason) => discovery.skipped.push(VerificationSkip {
                resource_id: resource.id.clone(),
                reason,
            }),
        }
    }

    let identities = stream::iter(probes)
        .map(|(id, probe)| async move { (id, probe.identity(host, federation).await) })
        .buffer_unordered(CONCURRENCY)
        .collect::<BTreeMap<_, _>>()
        .await;
    discovery.unavailable_local_count = local_projects
        .values()
        .flat_map(BTreeMap::keys)
        .filter(|id| identities.get(*id).is_some_and(Option::is_none))
        .count();
    let identity = |id: &str| identities.get(id).and_then(Option::as_ref);
    for (resource, compatible) in planned {
        let locals = compatible
            .values()
            .map(|local| (&local.target, identity(&local.target.id)));
        match match_identity(
            identity(&resource.id),
            locals,
            resource.association.as_ref(),
        ) {
            Ok((local, already_linked)) => discovery.candidates.push(VerifiedCandidate {
                resource_id: resource.id.clone(),
                local: local.clone(),
                local_db: compatible[&local.id].record.logical_db().into(),
                cloud_db: resource.db.clone(),
                already_linked,
            }),
            Err(reason) => discovery.skipped.push(VerificationSkip {
                resource_id: resource.id.clone(),
                reason,
            }),
        }
    }
    discovery
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn row() -> Value {
        json!({
            "database_name": "actual_database",
            "database_oid": "16384",
            "system_id": "7512345678901234567",
            "server_address": "192.0.2.20/32",
            "server_port": 5432,
            "instance_start": "1780725670.123456",
        })
    }

    fn target(key: &str) -> LocalTarget {
        LocalTarget {
            id: format!("local:{key}"),
            workspace: "/workspace/Example".into(),
            key: key.into(),
        }
    }

    #[test]
    fn matches_exact_database_with_unrelated_display_names() {
        let local = target("local-label-unrelated-to-actual-database");
        let local_identity = PostgresIdentity::from_rows(&[row()]).unwrap();
        let mut cloud_row = row();
        cloud_row["server_address"] = json!("192.0.2.20");
        let cloud_identity = PostgresIdentity::from_rows(&[cloud_row]).unwrap();
        let matches = [(&local, Some(&local_identity))];
        assert_eq!(
            match_identity(Some(&cloud_identity), matches, None),
            Ok((&local, false))
        );
        assert_eq!(
            match_identity(Some(&cloud_identity), matches, Some(&local)),
            Ok((&local, true))
        );
        assert_eq!(
            match_identity(Some(&cloud_identity), matches, Some(&target("different"))),
            Err(SkipReason::ConflictingAssociation)
        );
    }

    #[test]
    fn different_database_or_clone_is_not_an_exact_match() {
        let local = target("local");
        let local_identity = PostgresIdentity::from_rows(&[row()]).unwrap();
        for (field, other) in [
            ("database_name", json!("other_database")),
            ("database_oid", json!("16385")),
            ("system_id", json!("7512345678901234568")),
            ("server_address", json!("192.0.2.21/32")),
            ("server_port", json!(5433)),
            ("instance_start", json!("1780725670.123457")),
        ] {
            let mut cloud_row = row();
            cloud_row[field] = other;
            let cloud_identity = PostgresIdentity::from_rows(&[cloud_row]).unwrap();
            assert_eq!(
                match_identity(
                    Some(&cloud_identity),
                    [(&local, Some(&local_identity))],
                    None
                ),
                Err(SkipReason::NoExactMatch),
                "{field}"
            );
        }
    }

    #[test]
    fn missing_or_incomplete_evidence_cannot_link() {
        let local = target("local");
        let identity = PostgresIdentity::from_rows(&[row()]).unwrap();
        for field in row().as_object().unwrap().keys() {
            let mut incomplete = row();
            incomplete[field] = Value::Null;
            assert!(
                PostgresIdentity::from_rows(&[incomplete]).is_none(),
                "{field}"
            );
        }
        assert!(PostgresIdentity::from_rows(&[]).is_none());
        assert!(PostgresIdentity::from_rows(&[row(), row()]).is_none());
        assert_eq!(
            match_identity(None, [(&local, Some(&identity))], None),
            Err(SkipReason::CloudIdentityUnavailable)
        );
        assert_eq!(
            match_identity(Some(&identity), [(&local, None)], None),
            Err(SkipReason::LocalIdentityUnavailable)
        );
    }

    #[test]
    fn an_unavailable_route_does_not_erase_a_proven_match() {
        let available = target("available");
        let unavailable = target("unavailable");
        let identity = PostgresIdentity::from_rows(&[row()]).unwrap();
        assert_eq!(
            match_identity(
                Some(&identity),
                [(&unavailable, None), (&available, Some(&identity))],
                None,
            ),
            Ok((&available, false))
        );
    }

    #[test]
    fn multiple_local_matches_require_explicit_selection() {
        let first = target("first");
        let second = target("second");
        let identity = PostgresIdentity::from_rows(&[row()]).unwrap();
        assert_eq!(
            match_identity(
                Some(&identity),
                [(&first, Some(&identity)), (&second, Some(&identity))],
                None
            ),
            Err(SkipReason::Ambiguous)
        );
    }
}
