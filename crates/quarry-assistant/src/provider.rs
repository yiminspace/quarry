use crate::{AssistantError, MAX_CONTEXT, MAX_RESPONSE, MAX_TOOL_CALLS, ModelConfig, Result};
use futures_util::StreamExt;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashSet},
    process::Stdio,
    sync::{Arc, Mutex},
};
use tokio::{
    io::AsyncReadExt,
    process::Command,
    sync::mpsc,
    time::{Instant, timeout},
};
use tokio_util::sync::CancellationToken;

pub(crate) struct Round {
    pub content: String,
    pub calls: Vec<Value>,
}
pub(crate) struct Sink<'a> {
    pub sender: &'a mpsc::Sender<Value>,
    pub envelope: &'a Value,
    pub accumulated: &'a Arc<Mutex<String>>,
}
impl Sink<'_> {
    async fn delta(&self, text: &str) -> Result<()> {
        {
            let mut all = self.accumulated.lock().unwrap_or_else(|e| e.into_inner());
            all.push_str(text);
            if all.chars().count() > MAX_RESPONSE {
                return Err(AssistantError::invalid("Model response is too large."));
            }
        }
        let mut event = self.envelope.clone();
        event["type"] = json!("text_delta");
        event["delta"] = json!(text);
        timeout(std::time::Duration::from_secs(3), self.sender.send(event))
            .await
            .map_err(|_| AssistantError::cancelled())?
            .map_err(|_| AssistantError::cancelled())
    }
}
fn invalid() -> AssistantError {
    AssistantError::invalid("Model endpoint returned an invalid response.")
}
fn string(value: &Value) -> Result<&str> {
    if value.is_null() {
        Ok("")
    } else {
        value.as_str().ok_or_else(invalid)
    }
}
fn validate(content: String, calls: Vec<Value>, finish: Option<&str>) -> Result<Round> {
    if !matches!(finish, Some("stop" | "tool_calls")) {
        return Err(AssistantError::invalid(
            "Model response was incomplete. No SQL changes were applied.",
        ));
    }
    if !calls.is_empty() && finish != Some("tool_calls") {
        return Err(AssistantError::invalid(
            "Model returned an incomplete tool request.",
        ));
    }
    if calls.is_empty() && finish == Some("tool_calls") {
        return Err(AssistantError::invalid(
            "Model returned an empty tool request.",
        ));
    }
    if calls.len() > MAX_TOOL_CALLS {
        return Err(AssistantError::invalid(
            "Model returned too many tool requests.",
        ));
    }
    let mut ids = HashSet::new();
    for c in &calls {
        let id = c["id"].as_str().ok_or_else(invalid)?;
        if id.is_empty() || id.chars().count() > 200 || !ids.insert(id) || c["type"] != "function" {
            return Err(AssistantError::invalid(
                "Model returned an invalid tool request.",
            ));
        }
        let f = &c["function"];
        if !matches!(
            f["name"].as_str(),
            Some("find_tables" | "describe_tables" | "propose_sql")
        ) || f["arguments"]
            .as_str()
            .is_none_or(|a| a.chars().count() > 16_000)
        {
            return Err(AssistantError::invalid(
                "Model requested an unsupported tool.",
            ));
        }
    }
    Ok(Round { content, calls })
}
fn network_error(_: reqwest::Error) -> AssistantError {
    AssistantError::unavailable("Cannot reach the model endpoint, or the response timed out.")
}
fn credential_error() -> AssistantError {
    AssistantError::unavailable(
        "Cannot obtain model credentials. Check the trusted local credential command and sign in again.",
    )
}

// No shell, inherited stdin, stderr capture, token cache or persistence. The
// executable is selected only by trusted local settings/embedding code.
async fn model_credential(config: &ModelConfig) -> Result<String> {
    let Some(argv) = &config.credential_command else {
        return Ok(config.api_key.clone());
    };
    config.validate()?;
    let mut child = Command::new(&argv[0])
        .args(&argv[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .map_err(|_| credential_error())?;
    let stdout = child.stdout.take().ok_or_else(credential_error)?;
    let operation = async {
        let read = async {
            let mut bytes = Vec::new();
            stdout
                .take(8195)
                .read_to_end(&mut bytes)
                .await
                .map_err(|_| credential_error())?;
            if bytes.len() > 8194 {
                return Err(credential_error());
            }
            Ok(bytes)
        };
        let wait = async { child.wait().await.map_err(|_| credential_error()) };
        let (status, bytes) = tokio::try_join!(wait, read)?;
        if !status.success() {
            return Err(credential_error());
        }
        let text = std::str::from_utf8(&bytes).map_err(|_| credential_error())?;
        let token = text
            .strip_suffix("\r\n")
            .or_else(|| text.strip_suffix('\n'))
            .unwrap_or(text);
        if token.is_empty()
            || token.len() > 8192
            || !token.bytes().all(|b| (0x21..=0x7e).contains(&b))
        {
            return Err(credential_error());
        }
        Ok(token.to_owned())
    };
    timeout(
        config.timeout.min(std::time::Duration::from_secs(10)),
        operation,
    )
    .await
    .map_err(|_| credential_error())?
}

#[cfg(all(test, unix))]
mod credential_tests {
    use super::*;
    use std::time::Duration;

    fn command(argv: &[&str]) -> ModelConfig {
        ModelConfig {
            base_url: "https://model.example/v1".into(),
            model: "fixture".into(),
            api_key: String::new(),
            credential_command: Some(argv.iter().map(|s| (*s).to_owned()).collect()),
            bifrost: false,
            thinking: "default".into(),
            timeout: Duration::from_secs(1),
        }
    }

    #[tokio::test]
    async fn credential_is_resolved_again_without_caching_or_persisting() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("token");
        std::fs::write(&path, "first-private-token\n").unwrap();
        let config = command(&["/bin/cat", path.to_str().unwrap()]);
        assert_eq!(
            model_credential(&config).await.unwrap(),
            "first-private-token"
        );
        std::fs::write(&path, "second-private-token\r\n").unwrap();
        assert_eq!(
            model_credential(&config).await.unwrap(),
            "second-private-token"
        );
        assert!(config.api_key.is_empty());
        assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 1);
        assert!(!format!("{config:?}").contains("private-token"));
    }

    #[tokio::test]
    async fn credential_errors_and_timeout_never_expose_process_output() {
        for script in [
            "exit 0",
            "printf private-sensitive-token; printf private-sensitive-error >&2; exit 1",
            "printf 'private-sensitive-token\\nsecond-line'",
            "printf 'private-sensitive-token\\t'",
            "printf ' private-sensitive-token'",
            "printf '\\377'",
            "while :; do printf private-sensitive-token; done",
            "printf private-sensitive-token; exec sleep 30",
        ] {
            let config = command(&["/bin/sh", "-c", script]);
            let started = Instant::now();
            let error = model_credential(&config).await.unwrap_err();
            assert!(started.elapsed() < Duration::from_secs(3));
            assert!(!format!("{error:?}").contains("private-sensitive"));
            assert!(!error.message.contains("private-sensitive"));
        }
        let error = model_credential(&command(&["/missing/private-sensitive-command"]))
            .await
            .unwrap_err();
        assert!(!error.message.contains("private-sensitive"));
    }

    #[tokio::test]
    async fn argv_is_passed_literally_without_a_shell() {
        let config = command(&["/usr/bin/printf", "%s", "$(private-sensitive-command)"]);
        assert_eq!(
            model_credential(&config).await.unwrap(),
            "$(private-sensitive-command)"
        );
    }
}
async fn wait<T>(
    future: impl std::future::Future<Output = Result<T>>,
    config: &ModelConfig,
    cancel: &CancellationToken,
    deadline: Instant,
) -> Result<T> {
    if Instant::now() >= deadline {
        return Err(AssistantError::invalid(
            "Model response exceeded the request time limit.",
        ));
    }
    let budget = config
        .timeout
        .min(deadline.saturating_duration_since(Instant::now()));
    tokio::select! {biased;_=cancel.cancelled()=>Err(AssistantError::cancelled()),result=timeout(budget,future)=>match result{Ok(r)=>r,Err(_)=>Err(AssistantError::unavailable("Cannot reach the model endpoint, or the response timed out."))}}
}
pub(crate) async fn round(
    config: &ModelConfig,
    messages: &[Value],
    tools: &[Value],
    session_id: &str,
    cancel: &CancellationToken,
    deadline: Instant,
    sink: Sink<'_>,
) -> Result<Round> {
    config.validate()?;
    let api_key = wait(model_credential(config), config, cancel, deadline).await?;
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(config.timeout)
        .build()
        .map_err(network_error)?;
    let mut payload = json!({"model":config.model,"messages":messages,"stream":true,"tools":tools,"temperature":0.3,"max_tokens":6000});
    let mut request = client
        .post(format!(
            "{}/chat/completions",
            config.base_url.trim_end_matches('/')
        ))
        .header("accept", "text/event-stream");
    if !api_key.is_empty() {
        request = request.bearer_auth(&api_key);
        if config.bifrost {
            request = request.header("x-bf-vk", &api_key);
        }
    }
    if config.bifrost {
        request = request.header("x-bf-lh-session-id", session_id);
    }
    if config.thinking == "disabled" {
        payload["thinking"] = json!({"type":"disabled"});
        if config.bifrost {
            request = request.header("x-bf-passthrough-extra-params", "true");
        }
    }
    let response = wait(
        async { request.json(&payload).send().await.map_err(network_error) },
        config,
        cancel,
        deadline,
    )
    .await?;
    if !response.status().is_success() {
        let status = response.status().as_u16();
        return Err(AssistantError::unavailable(match status {
            401 | 403 => "Model authentication failed. Check `qy ai show` and your API key.".into(),
            429 => "Model request limit reached. Try again later.".into(),
            _ => format!("Model endpoint returned HTTP {status}. Check `qy ai show`."),
        }));
    }
    let is_json = response
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .contains("application/json");
    let mut stream = response.bytes_stream();
    let mut wire = 0usize;
    let mut buffer = Vec::new();
    let mut data_lines = Vec::<Vec<u8>>::new();
    let mut content = String::new();
    let mut calls = BTreeMap::<usize, Value>::new();
    let mut finish = None::<String>;
    let mut terminated = false;
    while let Some(bytes) = wait(
        async { stream.next().await.transpose().map_err(network_error) },
        config,
        cancel,
        deadline,
    )
    .await?
    {
        wire = wire.checked_add(bytes.len()).ok_or_else(invalid)?;
        if wire > if is_json { MAX_CONTEXT } else { 2_000_000 } {
            return Err(AssistantError::invalid("Model response is too large."));
        }
        buffer.extend_from_slice(&bytes);
        if is_json {
            continue;
        }
        while let Some(newline) = buffer.iter().position(|b| *b == b'\n') {
            let mut line = buffer.drain(..=newline).collect::<Vec<_>>();
            line.pop();
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            if line.len() > MAX_CONTEXT {
                return Err(AssistantError::invalid("Model response is too large."));
            }
            if let Some(data) = line.strip_prefix(b"data:") {
                data_lines.push(data.strip_prefix(b" ").unwrap_or(data).to_vec());
                continue;
            }
            if !line.is_empty() || data_lines.is_empty() {
                continue;
            }
            let raw = std::mem::take(&mut data_lines).join(&b'\n');
            if raw
                .iter()
                .copied()
                .filter(|b| !b.is_ascii_whitespace())
                .eq(b"[DONE]".iter().copied())
            {
                terminated = true;
                break;
            }
            let data: Value = serde_json::from_slice(&raw).map_err(|_| invalid())?;
            if data.get("error").is_some() {
                return Err(AssistantError::invalid(
                    "Model provider reported an error. Check your model configuration.",
                ));
            }
            let Some(choice) = data["choices"].as_array().and_then(|a| a.first()) else {
                continue;
            };
            let delta = &choice["delta"];
            let text = string(&delta["content"])?;
            if !text.is_empty() {
                content.push_str(text);
                if content.chars().count() > MAX_RESPONSE {
                    return Err(AssistantError::invalid("Model response is too large."));
                }
                sink.delta(text).await?;
            }
            if let Some(parts) = delta.get("tool_calls") {
                for part in parts.as_array().ok_or_else(invalid)? {
                    let index = part["index"].as_u64().ok_or_else(invalid)? as usize;
                    if index >= MAX_TOOL_CALLS {
                        return Err(AssistantError::invalid(
                            "Model returned too many tool requests.",
                        ));
                    }
                    let target = calls.entry(index).or_insert_with(
                        || json!({"id":"","type":"function","function":{"name":"","arguments":""}}),
                    );
                    let id = string(&part["id"])?;
                    target["id"] = json!(target["id"].as_str().unwrap().to_owned() + id);
                    for key in ["name", "arguments"] {
                        let addition = string(&part["function"][key])?;
                        let combined =
                            target["function"][key].as_str().unwrap().to_owned() + addition;
                        if combined.chars().count() > 16_000 {
                            return Err(AssistantError::invalid(
                                "Model tool arguments are too large.",
                            ));
                        }
                        target["function"][key] = json!(combined);
                    }
                }
            }
            if !choice["finish_reason"].is_null() {
                finish = Some(
                    choice["finish_reason"]
                        .as_str()
                        .ok_or_else(invalid)?
                        .to_owned(),
                );
            }
        }
        if terminated {
            break;
        }
        if buffer.len() > MAX_CONTEXT {
            return Err(AssistantError::invalid("Model response is too large."));
        }
    }
    if cancel.is_cancelled() {
        return Err(AssistantError::cancelled());
    }
    if is_json {
        let data: Value = serde_json::from_slice(&buffer).map_err(|_| invalid())?;
        let choice = data["choices"]
            .as_array()
            .and_then(|a| a.first())
            .ok_or_else(invalid)?;
        let msg = &choice["message"];
        let content = string(&msg["content"])?.to_owned();
        if content.chars().count() > MAX_RESPONSE {
            return Err(AssistantError::invalid("Model response is too large."));
        }
        let calls = if msg["tool_calls"].is_null() {
            Vec::new()
        } else {
            msg["tool_calls"].as_array().ok_or_else(invalid)?.clone()
        };
        let complete = validate(content, calls, choice["finish_reason"].as_str())?;
        if !complete.content.is_empty() {
            sink.delta(&complete.content).await?;
        }
        return Ok(complete);
    }
    if !terminated {
        return Err(AssistantError::invalid(
            "Model stream ended early. No SQL changes were applied.",
        ));
    }
    validate(content, calls.into_values().collect(), finish.as_deref())
}
