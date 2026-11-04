use crate::service::{Failure, Host, Result};
use quarry_core::{CancellationToken, QueryRequest};
use quarry_runtime::{QueryStore, SavedQuery};
use quarry_service::MetadataOperation;
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, time::Instant};
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncWriteExt, BufReader};

fn tools() -> Value {
    serde_json::from_str(include_str!("../assets/mcp-tools.json")).expect("Bundled MCP schemas")
}
fn required<'a>(args: &'a Value, key: &str) -> Result<&'a str> {
    args[key]
        .as_str()
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| Failure::invalid(format!("Missing tool argument {key}")))
}
fn max_rows(args: &Value) -> Result<Option<usize>> {
    match args.get("max_rows") {
        None => Ok(Some(500)),
        Some(value) => {
            let n = value
                .as_u64()
                .ok_or_else(|| Failure::invalid("max_rows must be a nonnegative integer"))?;
            let n = usize::try_from(n).map_err(|_| Failure::invalid("max_rows is too large"))?;
            Ok((n > 0).then_some(n))
        }
    }
}
async fn bounded_line(reader: &mut (impl AsyncBufRead + Unpin)) -> Result<Option<Vec<u8>>> {
    let mut line = Vec::new();
    loop {
        let available = reader.fill_buf().await?;
        if available.is_empty() {
            return Ok((!line.is_empty()).then_some(line));
        }
        let count = available
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(available.len(), |position| position + 1);
        if line.len() + count > 1_048_576 {
            return Err(Failure::invalid("MCP message exceeds limit"));
        }
        let done = available[count - 1] == b'\n';
        line.extend_from_slice(&available[..count]);
        reader.consume(count);
        if done {
            return Ok(Some(line));
        }
    }
}
pub async fn serve(host: Host, allow_write: bool) -> Result<()> {
    let mut input = BufReader::new(tokio::io::stdin());
    let mut stdout = tokio::io::stdout();
    let result=async {
        while let Some(line)=bounded_line(&mut input).await? {
            let request:Value=match serde_json::from_slice(&line){Ok(value)=>value,Err(_)=>{stdout.write_all(b"{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":-32700,\"message\":\"Parse error\"}}\n").await?;stdout.flush().await?;continue;}};
            let Some(response)=dispatch(&host,request,allow_write).await else {continue;};
            stdout.write_all(format!("{response}\n").as_bytes()).await?;stdout.flush().await?;
        }
        Ok(())
    }.await;
    host.tunnels.close_all().await?;
    result
}
async fn dispatch(host: &Host, request: Value, allow_write: bool) -> Option<Value> {
    let id = request.get("id").cloned();
    let valid = request.is_object()
        && request["jsonrpc"] == "2.0"
        && request["method"].is_string()
        && id
            .as_ref()
            .is_none_or(|value| value.is_string() || value.is_number() || value.is_null());
    if !valid {
        return Some(
            json!({"jsonrpc":"2.0","id":id.filter(|value|value.is_string()||value.is_number()).unwrap_or(Value::Null),"error":{"code":-32600,"message":"Invalid Request"}}),
        );
    }
    let id = id?; // notifications never receive a response
    let params = request.get("params").cloned().unwrap_or(json!({}));
    let response = match request["method"].as_str().unwrap() {
        "initialize" => {
            json!({"result":{"protocolVersion":params["protocolVersion"].as_str().unwrap_or("2025-06-18"),"capabilities":{"tools":{}},"serverInfo":{"name":"quarry","version":env!("CARGO_PKG_VERSION")}}})
        }
        "ping" => json!({"result":{}}),
        "tools/list" => json!({"result":{"tools":tools()}}),
        "resources/list" => json!({"result":{"resources":[]}}),
        "resources/templates/list" => json!({"result":{"resourceTemplates":[]}}),
        "prompts/list" => json!({"result":{"prompts":[]}}),
        "tools/call" => {
            let args = params.get("arguments").cloned().unwrap_or(json!({}));
            let result = if !args.is_object() {
                Err(Failure::invalid("Tool arguments must be an object"))
            } else {
                tool(
                    host,
                    params["name"].as_str().unwrap_or(""),
                    args,
                    allow_write,
                )
                .await
            };
            match result {
                Ok(value) => {
                    json!({"result":{"content":[{"type":"text","text":value.to_string()}],"structuredContent":value,"isError":false}})
                }
                Err(error) => {
                    json!({"result":{"content":[{"type":"text","text":json!({"error":error.message,"code":error.code}).to_string()}],"isError":true}})
                }
            }
        }
        _ => json!({"error":{"code":-32601,"message":"Method not found"}}),
    };
    let mut response = response.as_object().cloned().unwrap();
    response.insert("id".into(), id);
    response.insert("jsonrpc".into(), json!("2.0"));
    Some(Value::Object(response))
}
async fn ping(host: &Host, args: &Value) -> Result<Value> {
    let seconds = match args.get("timeout") {
        None => 10,
        Some(value) => value
            .as_u64()
            .filter(|n| (1..=30).contains(n))
            .ok_or_else(|| {
                Failure::invalid("timeout must be an integer between 1 and 30 seconds")
            })?,
    };
    let record = host.resolve(
        required(args, "db")?,
        args["env"].as_str(),
        args["workspace"].as_str().map(Path::new),
    )?;
    let started = Instant::now();
    let result = host
        .metadata(
            &record,
            MetadataOperation::Ping,
            Some(seconds * 1000),
            CancellationToken::new(),
        )
        .await;
    match result {
        Ok(mut value) => {
            value["db"] = json!(record.logical_db());
            value["env"] = json!(record.display_env());
            value["production"] = json!(record.production);
            Ok(value)
        }
        Err(error) => {
            // Driver/SSH messages can contain private endpoints or key paths.
            let lower = error.message.to_lowercase();
            let (kind, message) = if error.code == "timeout"
                || lower.contains("timed out")
                || lower.contains("timeout")
            {
                ("timeout", "Connection probe timed out.")
            } else if [
                "authentication",
                "access denied",
                "password",
                "permission denied",
            ]
            .iter()
            .any(|value| lower.contains(value))
            {
                ("authentication", "Connection authentication failed.")
            } else if lower.contains("ssh") || lower.contains("tunnel") {
                ("ssh", "SSH tunnel setup failed.")
            } else if lower.contains("not found") || lower.contains("not installed") {
                (
                    "dependency_or_file",
                    "A required executable or file is unavailable.",
                )
            } else {
                ("connection", "Connection probe failed.")
            };
            Ok(
                json!({"ok":false,"elapsed_ms":started.elapsed().as_millis(),"db":record.logical_db(),"env":record.display_env(),"production":record.production,"error_kind":kind,"error":message}),
            )
        }
    }
}
async fn tool(host: &Host, name: &str, args: Value, allow_write: bool) -> Result<Value> {
    let store = QueryStore::new(host.catalog()?);
    match name {
        "list_connections" => host.connections().await,
        "ping_connection" => ping(host, &args).await,
        "list_tables" | "describe_table" => {
            let record = host.resolve(
                required(&args, "db")?,
                args["env"].as_str(),
                args["workspace"].as_str().map(Path::new),
            )?;
            let engine = record.spec()?.engine;
            if name == "list_tables" {
                let mut result = host.tables(&record).await?;
                if engine == quarry_core::Engine::Redis {
                    result["keys"] = json!(
                        result["keys"]
                            .as_array()
                            .map(|keys| keys
                                .iter()
                                .map(|entry| entry["key"].clone())
                                .collect::<Vec<_>>())
                            .unwrap_or_default()
                    );
                }
                Ok(result)
            } else {
                if !engine.capabilities().schema {
                    return Err(Failure::invalid(
                        "describe_table requires PostgreSQL, MySQL or SQLite",
                    ));
                }
                let table = required(&args, "table")?;
                let data = host.columns(&record, table).await?;
                Ok(
                    json!({"table":table,"engine":engine.as_str(),"columns":data["rows"],"primary_key":data["primary_key"],"foreign_keys":data["foreign_keys"],"capped":data["capped"]}),
                )
            }
        }
        "exec_sql" => {
            let record = host.resolve(
                required(&args, "db")?,
                args["env"].as_str(),
                args["workspace"].as_str().map(Path::new),
            )?;
            let mut request = QueryRequest::new(required(&args, "sql")?);
            if args["write"] == true && !allow_write {
                return Err(Failure {
                    status: 403,
                    code: "safety".into(),
                    message: "Writes are disabled for this MCP server".into(),
                });
            }
            request.policy.allow_write = allow_write && args["write"] == true;
            request.policy.confirm_production = args["confirm_prod"] == true;
            request.policy.max_rows = max_rows(&args)?;
            request.policy.timeout_ms = Some(120_000);
            Ok(host.execute(&record, request).await?.to_legacy_json())
        }
        "list_saved_queries" => {
            let inventory = store.scan()?;
            let issues=inventory.issues.iter().map(|issue|json!({"path":crate::service::display_path(&issue.path),"workspace":crate::service::display_path(&issue.workspace),"code":issue.code,"reason":issue.reason})).collect::<Vec<_>>();
            Ok(
                json!({"queries":inventory.queries.into_iter().filter(|query|args["db"].as_str().is_none_or(|db|query.db==db)).collect::<Vec<_>>(),"issues":issues,"omittedIssues":inventory.omitted_issues}),
            )
        }
        "describe_saved_query" => Ok(serde_json::to_value(store.describe(
            required(&args, "name")?,
            args["workspace"].as_str().map(Path::new),
        )?)?),
        "run_saved_query" => {
            let query = store.describe(
                required(&args, "name")?,
                args["workspace"].as_str().map(Path::new),
            )?;
            let record = host.resolve_related(&query.db, args["env"].as_str(), &query.workspace)?;
            let values = args["params"]
                .as_object()
                .map(|map| {
                    map.iter()
                        .map(|(key, value)| {
                            (
                                key.clone(),
                                value
                                    .as_str()
                                    .map(String::from)
                                    .unwrap_or(value.to_string()),
                            )
                        })
                        .collect::<BTreeMap<_, _>>()
                })
                .unwrap_or_default();
            let mut request = QueryRequest::new(&query.sql);
            request.parameters = query.parameters(&values)?;
            request.policy.timeout_ms = Some(120_000);
            request.policy.max_rows = max_rows(&args)?;
            Ok(host.execute(&record, request).await?.to_legacy_json())
        }
        "save_query" => {
            let catalog = host.catalog()?;
            let workspace = match args["workspace"].as_str() {
                Some(path) => quarry_runtime::absolute(Path::new(path), &std::env::current_dir()?)?,
                None if catalog.workspaces.len() == 1 => catalog.workspaces[0].home.clone(),
                _ => {
                    return Err(Failure::invalid(
                        "Select an explicit workspace to save a query",
                    ));
                }
            };
            let name = required(&args, "name")?;
            let db = required(&args, "db")?;
            if !catalog.connections()?.iter().any(|record| {
                record.workspace == workspace && (record.logical_db() == db || record.key == db)
            }) {
                return Err(Failure::invalid(
                    "Database does not belong to the selected workspace",
                ));
            }
            if store
                .list()?
                .iter()
                .any(|query| query.name == name && (query.workspace != workspace || query.db != db))
            {
                return Err(Failure::invalid(
                    "Query name already exists elsewhere; choose a unique name",
                ));
            }
            let query = SavedQuery {
                name: name.into(),
                db: db.into(),
                desc: args["desc"].as_str().unwrap_or("").into(),
                tags: args["tags"]
                    .as_array()
                    .map(|values| {
                        values
                            .iter()
                            .filter_map(Value::as_str)
                            .map(String::from)
                            .collect()
                    })
                    .unwrap_or_default(),
                params: serde_json::from_value(args.get("params").cloned().unwrap_or(json!([])))?,
                schema_sources: vec![],
                source_fingerprint: None,
                saved_at: None,
                last_validated: None,
                sql: required(&args, "sql")?.into(),
                path: Default::default(),
                workspace: workspace.clone(),
            };
            let query = store.save(&workspace, query, args["overwrite"] == true)?;
            Ok(
                json!({"path":query.path,"name":query.name,"saved_at":query.saved_at,"last_validated":query.last_validated,"saved":true}),
            )
        }
        _ => Err(Failure::invalid("Unknown tool")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (tempfile::TempDir, Host) {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("connections.toml"),"[notes]\nurl='sqlite:///notes.db'\n[missing]\nurl='sqlite:///private-missing-database.db'\n").unwrap();
        std::fs::write(dir.path().join("notes.db"), b"").unwrap();
        let host = Host::new(
            dir.path().join("config.toml"),
            vec![dir.path().to_path_buf()],
            dir.path().join("state"),
        );
        (dir, host)
    }
    #[tokio::test]
    async fn ping_validates_timeout_and_redacts_failed_connection_details() {
        let (_dir, host) = fixture();
        for value in [json!(0), json!(31), json!(1.5), json!("2"), json!(true)] {
            assert!(
                tool(
                    &host,
                    "ping_connection",
                    json!({"db":"notes","timeout":value}),
                    false
                )
                .await
                .is_err()
            );
        }
        let success = tool(
            &host,
            "ping_connection",
            json!({"db":"notes","timeout":1}),
            false,
        )
        .await
        .unwrap();
        assert_eq!(success["ok"], true);
        assert_eq!(success["env"], Value::Null);
        let failure = tool(
            &host,
            "ping_connection",
            json!({"db":"missing","timeout":1}),
            false,
        )
        .await
        .unwrap();
        assert_eq!(failure["ok"], false);
        assert!(failure["error_kind"].is_string());
        assert!(!failure.to_string().contains("private-missing"));
    }
    #[tokio::test]
    async fn protocol_discovery_notifications_and_line_budget_are_complete() {
        let (_dir, host) = fixture();
        assert!(
            dispatch(
                &host,
                json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
                false
            )
            .await
            .is_none()
        );
        let init=dispatch(&host,json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}),false).await.unwrap();
        assert_eq!(init["result"]["protocolVersion"], "2024-11-05");
        for (method, key) in [
            ("resources/list", "resources"),
            ("resources/templates/list", "resourceTemplates"),
            ("prompts/list", "prompts"),
        ] {
            let response = dispatch(
                &host,
                json!({"jsonrpc":"2.0","id":2,"method":method}),
                false,
            )
            .await
            .unwrap();
            assert_eq!(response["result"][key], json!([]));
        }
        let response=dispatch(&host,json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"exec_sql","arguments":{"db":"notes","sql":"DELETE FROM t","write":true}}}),false).await.unwrap();
        assert_eq!(response["result"]["isError"], true);
        let mut reader = BufReader::new(&b"one\ntwo"[..]);
        assert_eq!(bounded_line(&mut reader).await.unwrap().unwrap(), b"one\n");
        assert_eq!(bounded_line(&mut reader).await.unwrap().unwrap(), b"two");
        assert!(bounded_line(&mut reader).await.unwrap().is_none());
        let data = vec![b'x'; 1_048_577];
        let mut reader = BufReader::new(data.as_slice());
        assert!(bounded_line(&mut reader).await.is_err());
    }
}
