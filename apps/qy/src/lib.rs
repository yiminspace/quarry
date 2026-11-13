pub mod federated_cli;
pub mod federation;
mod gui_launch;
pub mod http;
mod mcp;
mod service;
mod tunnel;
mod updater;
mod verified_associations;
use clap::{Args, CommandFactory, FromArgMatches, Parser, Subcommand};
use federated_cli::{Catalog, Selector, Source, Target};
use federation::Federation;
use quarry_core::{CancellationToken, Engine, QueryOutcome, QueryRequest};
use quarry_runtime::{QueryParameter, QueryStore, RuntimeConfig, SavedQuery};
use quarry_service::MetadataOperation;
use serde_json::{Value, json};
use service::{Failure, Host, Result};
use std::{
    collections::{BTreeMap, HashMap},
    io::{self, IsTerminal, Read, Write},
    path::{Path, PathBuf},
    sync::Arc,
    time::Instant,
};

#[derive(Parser)]
#[command(
    name = "qy",
    version,
    about = "Quarry database workbench and reusable Rust execution services"
)]
struct Cli {
    #[arg(long, global = true)]
    workspace: Vec<PathBuf>,
    #[arg(long, global = true, env = "QUARRY_CONFIG")]
    config: Option<PathBuf>,
    #[arg(long, global = true)]
    skill_dir: Option<PathBuf>,
    #[command(subcommand)]
    command: Command,
}
#[derive(Args)]
struct QueryOptions {
    #[arg(long)]
    env: Option<String>,
    #[arg(long)]
    source: Option<Source>,
    #[arg(long)]
    write: bool,
    #[arg(long)]
    yes: bool,
    #[arg(long, default_value_t = 500)]
    max_rows: usize,
    #[arg(long, default_value_t = 0)]
    offset: usize,
    #[arg(long,value_parser=clap::value_parser!(u64).range(1..))]
    timeout: Option<u64>,
    #[arg(long, default_value = "json", value_parser=["json","ndjson","csv","table"])]
    format: String,
    #[arg(long = "param")]
    parameters: Vec<String>,
    #[arg(long)]
    no_proxy: bool,
}
#[derive(Subcommand)]
enum Command {
    Exec {
        db: String,
        #[arg(long)]
        sql: Option<String>,
        #[arg(long)]
        file: Option<PathBuf>,
        params: Vec<String>,
        #[command(flatten)]
        options: QueryOptions,
    },
    Run {
        name: String,
        /// Select a compatible execution target from `qy connections`.
        #[arg(long)]
        target: Option<String>,
        params: Vec<String>,
        #[arg(long)]
        strict: bool,
        #[arg(long)]
        full: bool,
        #[arg(long)]
        limit: Option<usize>,
        #[command(flatten)]
        options: QueryOptions,
    },
    Connections {
        #[command(subcommand)]
        action: Option<ConnectionCommand>,
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    /// Register installed database provider helpers for this Quarry configuration.
    Provider {
        #[command(subcommand)]
        action: ProviderCommand,
    },
    Schema {
        db: String,
        table: String,
        #[arg(long)]
        env: Option<String>,
        #[arg(long)]
        source: Option<Source>,
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    #[command(name = "describe-table")]
    DescribeTable {
        db: String,
        table: String,
        #[arg(long)]
        env: Option<String>,
        #[arg(long)]
        source: Option<Source>,
        #[arg(long, default_value="json", value_parser=["json","text","table"])]
        format: String,
    },
    Tables {
        db: String,
        #[arg(long)]
        env: Option<String>,
        #[arg(long)]
        source: Option<Source>,
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    Ping {
        key: Option<String>,
        #[arg(long)]
        all: bool,
        #[arg(long)]
        env: Option<String>,
        #[arg(long)]
        source: Option<Source>,
        #[arg(long)]
        timeout: Option<u64>,
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    List {
        #[arg(long)]
        db: Option<String>,
        #[arg(long)]
        tag: Option<String>,
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    Describe {
        name: String,
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    Save {
        name: String,
        #[arg(long)]
        db: String,
        #[arg(long)]
        sql: Option<String>,
        #[arg(long)]
        file: Option<PathBuf>,
        #[arg(long, default_value = "")]
        desc: String,
        #[arg(long, default_value = "")]
        tags: String,
        #[arg(long = "param")]
        params: Vec<String>,
        #[arg(long = "schema-source")]
        schema_sources: Vec<String>,
        #[arg(long)]
        overwrite: bool,
        #[arg(long)]
        no_validate: bool,
    },
    Validate {
        name: String,
    },
    Fingerprint {
        name: String,
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    Audit {
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    Remove {
        name: String,
        #[arg(long)]
        yes: bool,
    },
    Edit {
        name: String,
    },
    Workspace {
        #[command(subcommand)]
        action: Option<WorkspaceCommand>,
    },
    Proxy {
        #[command(subcommand)]
        action: Option<ProxyCommand>,
    },
    Up,
    Down,
    /// Install the latest verified native release.
    Update,
    Status {
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    Local {
        #[command(subcommand)]
        action: LocalCommand,
    },
    /// Open the local browser workbench.
    Gui {
        #[arg(long, global = true, default_value = "127.0.0.1")]
        host: String,
        #[arg(long, global = true, default_value_t = 8765)]
        port: u16,
        #[arg(long)]
        no_browser: bool,
        #[command(subcommand)]
        action: Option<GuiCommand>,
    },
    Mcp {
        #[arg(long)]
        write: bool,
    },
    Ai {
        #[command(subcommand)]
        action: AiCommand,
    },
    Speedtest {
        db: String,
        #[arg(long)]
        env: Option<String>,
        #[arg(long, default_value_t = 1_000_000)]
        bytes: usize,
        #[arg(long, default_value_t = 3)]
        runs: usize,
        #[arg(long)]
        timeout: Option<u64>,
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    #[command(hide = true)]
    InternalKeeper {
        #[arg(long)]
        state_dir: PathBuf,
    },
    #[command(hide = true)]
    InternalNeptune {
        #[arg(long)]
        state_dir: PathBuf,
        #[arg(long)]
        port: u16,
        #[arg(long)]
        fixture: Option<PathBuf>,
    },
}
#[derive(Subcommand)]
enum GuiCommand {
    /// Gracefully stop the matching standalone workbench.
    Stop,
}
#[derive(Subcommand)]
enum ConnectionCommand {
    List {
        #[arg(long, value_parser=["json","text","table"])]
        format: Option<String>,
    },
    Add(ConnectionFields),
    Set(ConnectionFields),
    Remove {
        key: String,
        #[arg(long)]
        yes: bool,
    },
    Test {
        key: String,
        #[arg(long)]
        env: Option<String>,
        #[arg(long)]
        source: Option<Source>,
    },
    /// List possible associations between hosted resources and local connections.
    Candidates,
    /// Verify which PostgreSQL routes reach the same database.
    Reconcile {
        /// Restrict verification to a project.
        #[arg(long)]
        project: Option<String>,
        /// Save verified associations that do not already have a link.
        #[arg(long)]
        apply: bool,
    },
    /// Associate one or more pairs of exact cloud and local IDs.
    Link {
        #[arg(required = true, num_args = 2.., value_names = ["CLOUD_ID", "LOCAL_ID"])]
        targets: Vec<String>,
        /// Apply a reviewed batch without an interactive prompt.
        #[arg(long)]
        yes: bool,
    },
    /// Remove an explicit association without removing either resource.
    Unlink {
        cloud: String,
    },
}
#[derive(Args)]
struct ConnectionFields {
    key: String,
    #[arg(long)]
    url: Option<String>,
    #[arg(long)]
    engine: Option<String>,
    #[arg(long)]
    db: Option<String>,
    #[arg(long)]
    env: Option<String>,
    #[arg(long)]
    group: Option<String>,
    #[arg(long)]
    region: Option<String>,
    #[arg(long)]
    notes: Option<String>,
    #[arg(long)]
    production: bool,
    #[arg(long)]
    no_production: bool,
    #[arg(long)]
    timeout: Option<u64>,
    #[arg(long)]
    ssh_host: Option<String>,
    #[arg(long)]
    ssh_user: Option<String>,
    #[arg(long)]
    ssh_key: Option<String>,
    #[arg(long)]
    ssh_port: Option<u16>,
    #[arg(long)]
    no_test: bool,
    #[arg(long)]
    force: bool,
}
#[derive(Subcommand)]
enum WorkspaceCommand {
    List {
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    Add {
        dir: PathBuf,
    },
    Remove {
        dir: PathBuf,
    },
}
#[derive(Subcommand)]
enum ProviderCommand {
    /// Verify a local helper and register its authorized database catalog.
    Register {
        #[arg(long)]
        id: String,
        #[arg(long)]
        label: String,
        #[arg(long)]
        origin: url::Url,
        #[arg(long)]
        profile: String,
        #[arg(long)]
        owner: String,
        #[arg(long)]
        executable: PathBuf,
        #[arg(last = true)]
        args: Vec<String>,
    },
}
#[derive(Subcommand)]
enum ProxyCommand {
    Status {
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    On,
    Off,
}
#[derive(Subcommand)]
enum LocalCommand {
    Up {
        db: Option<String>,
        #[arg(long)]
        engine: Option<String>,
        #[arg(long)]
        image: Option<String>,
        #[arg(long)]
        port: Option<u16>,
        #[arg(long)]
        fixture: Option<PathBuf>,
    },
    Down {
        #[arg(long, default_value = "all")]
        engine: String,
        #[arg(long)]
        purge: bool,
        #[arg(long)]
        yes: bool,
    },
    Status {
        #[arg(long)]
        engine: Option<String>,
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
    Sync {
        db: String,
        #[arg(long = "from", default_value = "dev")]
        from: String,
    },
    /// Expose a cloud Local on a loopback port for your own services.
    Connect {
        /// Hosted Quarry address, e.g. https://quarry.example.com/quarry/
        server: String,
        /// Tunnel ticket copied from the workbench (or QUARRY_TUNNEL_TICKET).
        #[arg(long, env = "QUARRY_TUNNEL_TICKET", hide_env_values = true)]
        ticket: String,
        #[arg(long, default_value_t = 5433)]
        port: u16,
    },
    #[command(name = "neptune-calls")]
    NeptuneCalls {
        #[arg(long, default_value_t = 0)]
        after: u64,
        #[arg(long, default_value = "json", value_parser=["json","text","table"])]
        format: String,
    },
}
#[derive(Subcommand)]
enum AiCommand {
    Show,
    Language {
        language: Option<String>,
    },
    Configure(AiConfigureOptions),
    Explain {
        #[arg(long)]
        db: String,
        #[arg(long)]
        env: Option<String>,
        #[arg(long)]
        sql: Option<String>,
        #[arg(long)]
        file: Option<PathBuf>,
        #[arg(long, default_value = "text", value_parser=["text","json"])]
        format: String,
    },
    Sql {
        prompt: String,
        #[arg(long)]
        db: String,
        #[arg(long)]
        env: Option<String>,
        #[arg(long)]
        sql: Option<String>,
        #[arg(long)]
        file: Option<PathBuf>,
        #[arg(long, default_value = "sql", value_parser=["sql","json"])]
        format: String,
    },
}
#[derive(Args)]
struct AiConfigureOptions {
    #[arg(long)]
    base_url: Option<String>,
    #[arg(long)]
    model: Option<String>,
    #[arg(long, conflicts_with_all=["api_key_stdin","no_key"])]
    api_key_env: Option<String>,
    #[arg(long, conflicts_with = "no_key")]
    api_key_stdin: bool,
    #[arg(long)]
    no_key: bool,
    #[arg(long)]
    bifrost: bool,
    #[arg(long, value_parser=["default","disabled"])]
    thinking: Option<String>,
    #[arg(long, default_value_t=30, value_parser=clap::value_parser!(u64).range(1..=300))]
    timeout: u64,
}
/// Run Quarry directly inside a host CLI. `args` includes the displayed program
/// name. `launcher_prefix` dispatches hidden daemon commands back into that host
/// (for example `["db", "local"]`), without a separately installed qy binary.
pub async fn run_from(args: Vec<std::ffi::OsString>, launcher_prefix: Vec<String>) -> Result<()> {
    run_embedded(args, launcher_prefix, None).await
}

/// Launch the complete desktop workbench with an additional hosted connection source.
pub async fn run_gui_from(
    args: Vec<std::ffi::OsString>,
    launcher_prefix: Vec<String>,
    cloud: Arc<dyn http::CloudBackend>,
    history: Option<http::NativeHistory>,
) -> Result<()> {
    run_embedded(args, launcher_prefix, Some((cloud, history))).await
}

fn parse_cli(args: &[std::ffi::OsString], standalone: bool) -> Result<Option<Cli>> {
    let program = args
        .first()
        .and_then(|a| a.to_str())
        .unwrap_or("qy")
        .to_owned();
    let mut command = Cli::command().bin_name(program);
    if !standalone {
        command = command
            .mut_subcommand("update", |command| command.hide(true))
            .mut_subcommand("gui", |command| {
                command.mut_subcommand("stop", |command| command.hide(true))
            });
    }
    let parsed = command
        .try_get_matches_from(args)
        .and_then(|m| Cli::from_arg_matches(&m));
    match parsed {
        Ok(cli) => Ok(Some(cli)),
        Err(error) => {
            if matches!(
                error.kind(),
                clap::error::ErrorKind::DisplayHelp | clap::error::ErrorKind::DisplayVersion
            ) {
                error.print()?;
                return Ok(None);
            }
            Err(Failure::invalid(error.to_string()))
        }
    }
}

/// Standalone executable entry point. Hosts using Quarry as a library keep
/// ownership of their own installation and updates.
pub fn run_standalone(args: Vec<std::ffi::OsString>) -> Result<()> {
    if let Some(result) = updater::internal_command(&args) {
        return result;
    }
    let Some(cli) = parse_cli(&args, true)? else {
        return Ok(());
    };
    match &cli.command {
        Command::Update => return updater::update_now(),
        Command::InternalKeeper { .. } | Command::InternalNeptune { .. } => {}
        _ => updater::on_start(&args),
    }
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(run_with_prefix(cli, Vec::new(), None, true))
}

async fn run_embedded(
    args: Vec<std::ffi::OsString>,
    launcher_prefix: Vec<String>,
    native: Option<(Arc<dyn http::CloudBackend>, Option<http::NativeHistory>)>,
) -> Result<()> {
    let Some(cli) = parse_cli(&args, false)? else {
        return Ok(());
    };
    if matches!(cli.command, Command::Update) {
        return Err(Failure::invalid(
            "Update Quarry through the application that embeds it",
        ));
    }
    if native.is_some() && !matches!(cli.command, Command::Gui { .. }) {
        return Err(Failure::invalid(
            "Cloud workbench integration requires the GUI command",
        ));
    }
    run_with_prefix(cli, launcher_prefix, native, false).await
}

fn home() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| Failure::invalid("HOME is not configured"))
}
fn read_sql(sql: Option<String>, file: Option<PathBuf>) -> Result<String> {
    match (sql, file) {
        (Some(_), Some(_)) => Err(Failure::invalid("Use either --sql or --file")),
        (Some(s), None) => Ok(s),
        (None, Some(p)) if p == Path::new("-") => {
            let mut s = String::new();
            io::stdin().take(1_048_577).read_to_string(&mut s)?;
            if s.len() > 1_048_576 {
                return Err(Failure::invalid("SQL input too large"));
            }
            Ok(s)
        }
        (None, Some(p)) => Ok(std::fs::read_to_string(p)?),
        _ => Err(Failure::invalid("Provide --sql or --file")),
    }
}
fn output(value: Value) -> Result<()> {
    serde_json::to_writer(io::stdout().lock(), &value)?;
    println!();
    Ok(())
}
fn metadata_output(value: Value, format: &str) -> Result<()> {
    match format {
        "json" => output(value),
        "text" | "table" => {
            io::stdout().write_all(metadata_text(&value).as_bytes())?;
            Ok(())
        }
        _ => Err(Failure::invalid("Unknown metadata output format")),
    }
}
fn display_cell(value: &Value) -> String {
    match value {
        Value::Null => "-".into(),
        Value::String(text) => text.split_whitespace().collect::<Vec<_>>().join(" "),
        Value::Array(items) => items
            .iter()
            .map(display_cell)
            .collect::<Vec<_>>()
            .join(", "),
        Value::Object(fields) => fields
            .iter()
            .map(|(key, value)| format!("{key}={}", display_cell(value)))
            .collect::<Vec<_>>()
            .join(", "),
        other => other.to_string(),
    }
}
fn text_table(rows: &[Value]) -> String {
    if rows.is_empty() {
        return "No entries.\n".into();
    }
    if rows.iter().any(|row| !row.is_object()) {
        return rows
            .iter()
            .map(|row| format!("{}\n", display_cell(row)))
            .collect();
    }
    let saved_queries = rows.iter().all(|row| {
        row.get("name").is_some() && row.get("db").is_some() && row.get("sql").is_some()
    });
    let mut headers = Vec::new();
    for row in rows {
        for key in row.as_object().unwrap().keys() {
            // Query lists summarize identities; `describe --format text` prints
            // complete SQL and metadata below instead of putting SQL in one cell.
            if saved_queries
                && !["name", "db", "desc", "tags", "params", "workspace"].contains(&key.as_str())
            {
                continue;
            }
            if !headers.contains(key) {
                headers.push(key.clone());
            }
        }
    }
    let cells = rows
        .iter()
        .map(|row| {
            headers
                .iter()
                .map(|key| display_cell(&row[key]))
                .collect::<Vec<_>>()
        })
        .collect::<Vec<_>>();
    let widths = headers
        .iter()
        .enumerate()
        .map(|(index, header)| {
            cells
                .iter()
                .map(|row| row[index].chars().count())
                .max()
                .unwrap_or(0)
                .max(header.chars().count())
        })
        .collect::<Vec<_>>();
    let line = |row: &[String]| {
        row.iter()
            .enumerate()
            .map(|(index, cell)| {
                format!(
                    "{cell}{}",
                    " ".repeat(widths[index].saturating_sub(cell.chars().count()))
                )
            })
            .collect::<Vec<_>>()
            .join(" | ")
    };
    let mut out = format!(
        "{}\n{}\n",
        line(&headers),
        widths
            .iter()
            .map(|width| "-".repeat(*width))
            .collect::<Vec<_>>()
            .join("-+-")
    );
    for row in cells {
        out.push_str(&line(&row));
        out.push('\n');
    }
    out
}
fn metadata_text(value: &Value) -> String {
    if let Some(groups) = value["groups"].as_array() {
        let mut out = String::new();
        for group in groups {
            out.push_str(&format!(
                "{} ({})\n",
                group["group"].as_str().unwrap_or("Other"),
                display_cell(&group["ws"])
            ));
            let mut rows = Vec::new();
            for item in group["items"].as_array().into_iter().flatten() {
                for env in item["envs"].as_array().into_iter().flatten() {
                    let mut row = json!({"database":item["db"],"environment":env["env"],"engine":env["engine"],"key":env["key"],"region":env["region"],"ssh":env["ssh"],"proxy":env["proxied"],"production":env["production"]});
                    if let Some(id) = env.get("resourceId") {
                        row["id"] = id.clone();
                        row["source"] = env["source"].clone();
                    }
                    if let Some(status) = env.get("status") {
                        row["status"] = status.clone();
                    }
                    rows.push(row);
                }
            }
            out.push_str(&text_table(&rows));
            out.push('\n');
        }
        return if out.is_empty() {
            "No connections.\n".into()
        } else {
            out
        };
    }
    if value.get("sql").is_some() && value.get("name").is_some() {
        let mut object = value.as_object().cloned().unwrap_or_default();
        let sql = object.remove("sql").unwrap_or_default();
        return format!(
            "{}\nSQL:\n{}\n",
            metadata_text(&Value::Object(object)),
            sql.as_str().unwrap_or("")
        );
    }
    if let Some(rows) = value["rows"].as_array() {
        let mut text = text_table(rows);
        if value["primary_key"]
            .as_array()
            .is_some_and(|keys| !keys.is_empty())
        {
            text.push_str(&format!(
                "Primary key: {}\n",
                display_cell(&value["primary_key"])
            ));
        }
        if value["foreign_keys"]
            .as_array()
            .is_some_and(|keys| !keys.is_empty())
        {
            text.push_str("Foreign keys:\n");
            text.push_str(&text_table(value["foreign_keys"].as_array().unwrap()));
        }
        return text;
    }
    match value {
        Value::Array(rows) => text_table(rows),
        Value::Object(fields) => {
            let mut out = String::new();
            for (key, value) in fields {
                if value.is_object() || value.is_array() {
                    out.push_str(&format!("{key}:\n"));
                    for line in metadata_text(value).lines() {
                        out.push_str("  ");
                        out.push_str(line);
                        out.push('\n');
                    }
                } else {
                    out.push_str(&format!("{key}: {}\n", display_cell(value)));
                }
            }
            out
        }
        value => format!("{}\n", display_cell(value)),
    }
}
fn warn_query_issues(inventory: &quarry_runtime::QueryInventory) {
    for issue in &inventory.issues {
        eprintln!(
            "Skipped saved query {}: {}",
            service::display_path(&issue.path),
            issue.reason
        );
    }
    if inventory.omitted_issues > 0 {
        eprintln!(
            "{} additional saved-query issues omitted",
            inventory.omitted_issues
        );
    }
}
fn primary(host: &Host) -> Result<PathBuf> {
    host.catalog()?
        .workspaces
        .first()
        .map(|w| w.home.clone())
        .ok_or_else(|| Failure::invalid("No workspace selected"))
}
fn params(values: &[String]) -> Result<BTreeMap<String, String>> {
    let mut p = BTreeMap::new();
    for v in values {
        let (k, v) = v
            .split_once('=')
            .ok_or_else(|| Failure::invalid("Parameters require name=value"))?;
        if p.insert(k.into(), v.into()).is_some() {
            return Err(Failure::invalid("Duplicate parameter"));
        }
    }
    Ok(p)
}
fn confirmation(message: &str, yes: bool) -> Result<()> {
    if yes {
        return Ok(());
    }
    if !io::stdin().is_terminal() {
        return Err(Failure::invalid(format!(
            "{message}; pass --yes explicitly"
        )));
    }
    eprint!("{message} [y/N] ");
    io::stderr().flush()?;
    let mut line = String::new();
    io::stdin().read_line(&mut line)?;
    if !matches!(line.trim().to_ascii_lowercase().as_str(), "y" | "yes") {
        return Err(Failure::invalid("Operation cancelled"));
    }
    Ok(())
}
fn assistant(host: &Host) -> Result<Arc<quarry_assistant::AssistantService>> {
    let system_language = system_reply_language();
    let manager = quarry_assistant::ConfigManager::new(
        host.config_path.clone(),
        std::env::vars().collect::<HashMap<_, _>>(),
        system_language,
    )
    .with_system_language_provider(system_reply_language);
    let path = std::env::var_os("QUARRY_AI_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            host.config_path
                .parent()
                .unwrap_or(Path::new("."))
                .join("assistant")
        })
        .join("history.sqlite3");
    Ok(Arc::new(quarry_assistant::AssistantService::new(
        quarry_assistant::ConversationStore::new(path),
        manager,
        Arc::new(host.clone()),
    )))
}
async fn run_with_prefix(
    cli: Cli,
    launcher_prefix: Vec<String>,
    native: Option<(Arc<dyn http::CloudBackend>, Option<http::NativeHistory>)>,
    standalone_updates: bool,
) -> Result<()> {
    let config_path = resolve_config_path(cli.config)?;
    if let Command::InternalKeeper { state_dir } = cli.command {
        let workspace = cli
            .workspace
            .first()
            .cloned()
            .ok_or_else(|| Failure::invalid("Keeper requires --workspace"))?;
        return Ok(quarry_runtime::run_keeper(config_path, workspace, state_dir).await?);
    }
    if let Command::InternalNeptune {
        state_dir,
        port,
        fixture,
    } = cli.command
    {
        return Ok(quarry_local::serve_neptune(state_dir, port, fixture).await?);
    }
    let host = create_host(config_path, cli.workspace, &launcher_prefix)?;
    if let Some(skill) = cli.skill_dir {
        host.catalog()?.skill_links(&skill)?;
    }
    let result = match cli.command {
        Command::Provider {
            action:
                ProviderCommand::Register {
                    id,
                    label,
                    origin,
                    profile,
                    owner,
                    executable,
                    args,
                },
        } => {
            let identity = federation::ProviderIdentity {
                id,
                label,
                origin: origin.to_string(),
                profile,
                owner,
            };
            let helper = federation::ProviderHelper { executable, args };
            let federation = Federation::new(&host.config_path)?;
            let count = federation.register(&identity, &helper).await?;
            output(json!({"registered": count}))
        }
        Command::Exec {
            db,
            sql,
            file,
            mut params,
            options,
        } => {
            params.extend(options.parameters.clone());
            let sql = read_sql(sql, file)?;
            let parameters = self::params(&params)?;
            let federation = target_federation(&host, options.source, Some(&db))?;
            let target = select_target(
                &host,
                federation.as_ref(),
                &db,
                options.env.as_deref(),
                options.source,
            )
            .await?;
            execute_target(
                &host,
                federation.as_ref(),
                &target,
                sql,
                parameters,
                &options,
            )
            .await
        }
        Command::Run {
            name,
            target,
            mut params,
            mut options,
            limit,
            full,
            strict,
        } => {
            let store = QueryStore::new(host.catalog()?);
            let query = store.describe(&name, None)?;
            params.extend(options.parameters.clone());
            let parameters = query.parameters(&self::params(&params)?)?;
            let federation = target_federation(&host, options.source, target.as_deref())?;
            let target = if let Some(reference) = target {
                let target = select_target(
                    &host,
                    federation.as_ref(),
                    &reference,
                    options.env.as_deref(),
                    options.source,
                )
                .await?;
                if !target.supports_saved_query(&host, &query.db, &query.workspace)? {
                    return Err(Failure::invalid(
                        "Saved query belongs to a different database",
                    ));
                }
                target
            } else if let Some(federation) = federation.as_ref() {
                let catalog = Catalog::for_saved(
                    &host,
                    federation,
                    &query.db,
                    options.env.as_deref(),
                    &query.workspace,
                    options.source,
                )
                .await?;
                catalog
                    .select(&Selector {
                        target: &query.db,
                        source: options.source,
                        env: options.env.as_deref(),
                        workspace: None,
                    })?
                    .clone()
            } else if options.source == Some(Source::Cloud) {
                return Err(Failure::invalid("No cloud database provider is configured"));
            } else {
                Target::local(host.resolve_related(
                    &query.db,
                    options.env.as_deref(),
                    &query.workspace,
                )?)
            };
            if full && target.source() == Source::Cloud {
                return Err(Failure::invalid("--full applies to local connections"));
            }
            if strict {
                let explain = format!("EXPLAIN {}", query.sql);
                match &target {
                    Target::Local { record, .. } => {
                        let mut request = QueryRequest::new(explain);
                        request.parameters = parameters.clone();
                        request.policy.timeout_ms = execution_timeout(record, options.timeout)?;
                        host.execute_with_proxy(record, request, options.no_proxy.then_some(false))
                            .await?;
                    }
                    Target::Cloud(_) => {
                        validate_cloud_query(&options)?;
                        let sql = quarry_core::policy::substitute(
                            target.engine()?,
                            &explain,
                            &parameters,
                        )?;
                        target
                            .execute(
                                &host,
                                federation.as_ref(),
                                &sql,
                                1,
                                0,
                                &uuid::Uuid::new_v4().to_string(),
                            )
                            .await?;
                    }
                }
            }
            let engine = target.engine()?;
            let sql = if full {
                options.max_rows = 0;
                quarry_core::policy::strip_outer_limit(&query.sql, engine)?
            } else if let Some(limit) = limit {
                options.max_rows = limit;
                quarry_core::policy::override_outer_limit(&query.sql, limit, engine)?
            } else {
                query.sql.clone()
            };
            execute_target(
                &host,
                federation.as_ref(),
                &target,
                sql,
                parameters,
                &options,
            )
            .await
        }
        Command::Connections { action, format } => connections(&host, action, &format).await,
        Command::Schema {
            db,
            table,
            env,
            source,
            format,
        }
        | Command::DescribeTable {
            db,
            table,
            env,
            source,
            format,
        } => {
            let federation = target_federation(&host, source, Some(&db))?;
            let target =
                select_target(&host, federation.as_ref(), &db, env.as_deref(), source).await?;
            metadata_output(
                target.columns(&host, federation.as_ref(), &table).await?,
                &format,
            )
        }
        Command::Tables {
            db,
            env,
            source,
            format,
        } => {
            let federation = target_federation(&host, source, Some(&db))?;
            let target =
                select_target(&host, federation.as_ref(), &db, env.as_deref(), source).await?;
            metadata_output(target.tables(&host, federation.as_ref()).await?, &format)
        }
        Command::Ping {
            key,
            all,
            env,
            source,
            timeout,
            format,
        } => {
            let federation =
                target_federation(&host, source, if all { None } else { key.as_deref() })?;
            let connections = if all {
                Catalog::new(&host, federation.as_ref())
                    .await?
                    .resources()
                    .iter()
                    .filter(|target| source.is_none_or(|source| source == target.source()))
                    .cloned()
                    .collect::<Vec<_>>()
            } else {
                let key = key
                    .as_deref()
                    .ok_or_else(|| Failure::invalid("Provide a database or --all"))?;
                vec![select_target(&host, federation.as_ref(), key, env.as_deref(), source).await?]
            };
            let deadline = timeout.map(seconds_to_millis).transpose()?;
            let mut statuses = vec![];
            for target in connections {
                let env = match &target {
                    Target::Local { record, .. } => Some(record.env.as_str()),
                    Target::Cloud(_) => target.env(),
                };
                let status = target.health(&host, federation.as_ref(), deadline).await;
                let mut result = match status {
                    Ok(v) => json!({"db":target.db(),"env":env,"result":v}),
                    Err(e) => json!({"db":target.db(),"env":env,"ok":false,"error":e.message}),
                };
                if federation.is_some() {
                    result["resourceId"] = json!(target.id());
                    result["source"] = json!(target.source().as_str());
                }
                statuses.push(result);
            }
            let failed = statuses
                .iter()
                .any(|v| v["ok"] == false || v["result"]["ok"] == false);
            metadata_output(json!({"items":statuses}), &format)?;
            if failed {
                Err(Failure::invalid("One or more probes failed"))
            } else {
                Ok(())
            }
        }
        Command::List { db, tag, format } => {
            let inventory = QueryStore::new(host.catalog()?).scan()?;
            warn_query_issues(&inventory);
            metadata_output(
                json!(
                    inventory
                        .queries
                        .into_iter()
                        .filter(|q| db.as_ref().is_none_or(|db| &q.db == db)
                            && tag.as_ref().is_none_or(|tag| q.tags.contains(tag)))
                        .collect::<Vec<_>>()
                ),
                &format,
            )
        }
        Command::Describe { name, format } => metadata_output(
            serde_json::to_value(QueryStore::new(host.catalog()?).describe(&name, None)?)?,
            &format,
        ),
        Command::Save {
            name,
            db,
            sql,
            file,
            desc,
            tags,
            params,
            schema_sources,
            overwrite,
            no_validate,
        } => {
            let store = QueryStore::new(host.catalog()?);
            let query = SavedQuery {
                name,
                db,
                desc,
                tags: tags
                    .split(',')
                    .filter(|s| !s.trim().is_empty())
                    .map(|s| s.trim().into())
                    .collect(),
                params: params
                    .iter()
                    .map(|value| parse_query_parameter(value))
                    .collect::<Result<Vec<_>>>()?,
                schema_sources,
                source_fingerprint: None,
                saved_at: None,
                last_validated: None,
                sql: read_sql(sql, file)?,
                path: PathBuf::new(),
                workspace: primary(&host)?,
            };
            let q = store.save(&primary(&host)?, query, overwrite)?;
            if !no_validate {
                let c = host.resolve_related(&q.db, None, &q.workspace)?;
                store
                    .validate_connected(&q, &c, BTreeMap::new(), &host.tunnels)
                    .await?;
            }
            output(json!({"path":q.path,"saved":true}))
        }
        Command::Validate { name } => {
            let store = QueryStore::new(host.catalog()?);
            let q = store.describe(&name, None)?;
            let c = host.resolve_related(&q.db, None, &q.workspace)?;
            output(serde_json::to_value(
                store
                    .validate_connected(&q, &c, BTreeMap::new(), &host.tunnels)
                    .await?,
            )?)
        }
        Command::Fingerprint { name, format } => {
            let store = QueryStore::new(host.catalog()?);
            let q = store.describe(&name, None)?;
            metadata_output(serde_json::to_value(store.fingerprint(&q)?)?, &format)
        }
        Command::Audit { format } => {
            metadata_output(json!(QueryStore::new(host.catalog()?).audit()?), &format)
        }
        Command::Remove { name, yes } => {
            confirmation("Remove saved query", yes)?;
            QueryStore::new(host.catalog()?).remove(&name, None)?;
            output(json!({"removed":name}))
        }
        Command::Edit { name } => {
            let q = QueryStore::new(host.catalog()?).describe(&name, None)?;
            let editor = std::env::var("EDITOR").unwrap_or_else(|_| "vi".into());
            let args =
                shell_words::split(&editor).map_err(|_| Failure::invalid("Invalid EDITOR"))?;
            let (program, args) = args
                .split_first()
                .ok_or_else(|| Failure::invalid("EDITOR is empty"))?;
            let status = std::process::Command::new(program)
                .args(args)
                .arg(q.path)
                .status()?;
            if status.success() {
                Ok(())
            } else {
                Err(Failure::invalid("Editor exited unsuccessfully"))
            }
        }
        Command::Workspace { action } => {
            let format = match &action {
                Some(WorkspaceCommand::List { format }) => format.clone(),
                _ => "json".into(),
            };
            let config = RuntimeConfig::load(&host.config_path)?;
            match action {
                Some(WorkspaceCommand::Add { dir }) => {
                    config.add_workspace(&dir)?;
                }
                Some(WorkspaceCommand::Remove { dir }) => {
                    config.remove_workspace(&dir)?;
                }
                _ => {}
            }
            metadata_output(host.workspaces()?, &format)
        }
        Command::Proxy { action } => {
            let format = match &action {
                Some(ProxyCommand::Status { format }) => format.clone(),
                _ => "json".into(),
            };
            if matches!(action, Some(ProxyCommand::On) | Some(ProxyCommand::Off)) {
                let config = RuntimeConfig::load(&host.config_path)?;
                config.set_toggle(
                    "proxy_enabled_workspaces",
                    &primary(&host)?,
                    matches!(action, Some(ProxyCommand::On)),
                )?;
            }
            metadata_output(
                json!({"workspaces":host.workspaces()?,"proxy":quarry_runtime::ProxyInfo::discover()}),
                &format,
            )
        }
        Command::Up => output(keepalive(&host, Some(true)).await?),
        Command::Down => output(keepalive(&host, Some(false)).await?),
        Command::Status { format } => metadata_output(keepalive(&host, None).await?, &format),
        Command::Local { action } => local(&host, action).await,
        Command::Gui {
            host: bind,
            port,
            no_browser,
            action,
        } => {
            if !matches!(bind.as_str(), "127.0.0.1" | "localhost" | "::1") {
                return Err(Failure::invalid("Local GUI must bind a loopback host"));
            }
            if matches!(action, Some(GuiCommand::Stop)) {
                if !standalone_updates {
                    return Err(Failure::invalid(
                        "Stop the embedded GUI through its host application",
                    ));
                }
                match gui_launch::stop(&host, &bind, port).await? {
                    Some(address) => println!("Stopping Quarry GUI at http://{address}/quarry/"),
                    None => println!("No matching Quarry GUI is running."),
                }
                return Ok(());
            }
            let federation = (standalone_updates && std::env::var_os("HOME").is_some())
                .then(|| federation::Federation::new(&host.config_path))
                .transpose()?;
            let protected = federation
                .as_ref()
                .map(|backend| backend.registry().has_providers())
                .transpose()?
                .unwrap_or(false);
            let launch = if standalone_updates {
                gui_launch::standalone(&host, &bind, port, !protected).await?
            } else {
                let listener = tokio::net::TcpListener::bind((bind.as_str(), port)).await?;
                gui_launch::Launch::Start(listener, None)
            };
            let (listener, gui_instance) = match launch {
                gui_launch::Launch::Start(listener, instance) => (listener, instance),
                gui_launch::Launch::Reuse(address) => {
                    let url = format!("http://{address}/quarry/");
                    eprintln!("Quarry GUI → {url}");
                    eprintln!("Reusing the running Quarry GUI.");
                    if !no_browser {
                        open_gui(&url);
                    }
                    return Ok(());
                }
            };
            let bound = listener.local_addr()?;
            let port = bound.port();
            let url = format!("http://{bound}/quarry/");
            let native = match native {
                Some((cloud, history)) => Some(http::NativeWorkbench::new(
                    cloud,
                    history.unwrap_or_else(|| http::NativeHistory::for_host(&host)),
                )),
                None => federation.map(|federation| {
                    http::NativeWorkbench::standalone(
                        federation,
                        http::NativeHistory::for_host(&host),
                        protected,
                    )
                }),
            };
            let launch_url = native
                .as_ref()
                .filter(|workbench| workbench.requires_session())
                .map(|workbench| workbench.launch_url(&url));
            let a = assistant(&host)?;
            let http_shutdown = quarry_core::CancellationToken::new();
            eprintln!(
                "Quarry GUI → {}",
                if no_browser {
                    launch_url.as_deref().unwrap_or(&url)
                } else {
                    &url
                }
            );
            if !no_browser {
                open_gui(launch_url.as_deref().unwrap_or(&url));
            }
            let state = http::WebState {
                host: host.clone(),
                assistant: a.clone(),
                port,
                shutdown: http_shutdown.clone(),
                native,
                standalone_updates,
                managed_install: standalone_updates && updater::is_managed_install(),
                gui_instance,
            };
            let shutdown = a.clone();
            let served = axum::serve(listener, http::router(state))
                .with_graceful_shutdown(async move {
                    tokio::select! {
                        _ = tokio::signal::ctrl_c() => (),
                        _ = http_shutdown.cancelled() => (),
                    }
                    http_shutdown.cancel();
                    shutdown.close();
                })
                .await;
            a.close();
            served?;
            Ok(())
        }
        Command::Mcp { write } => mcp::serve(host.clone(), write).await,
        Command::Ai { action } => ai(&host, action).await,
        Command::Speedtest {
            db,
            env,
            bytes,
            runs,
            timeout,
            format,
        } => {
            if bytes == 0 || bytes > 100_000_000 || runs == 0 || runs > 20 {
                return Err(Failure::invalid(
                    "Speedtest bytes/runs exceed supported bounds",
                ));
            }
            let c = host.resolve(&db, env.as_deref(), None)?;
            let query = match c.spec()?.engine {
                Engine::Postgres => format!("SELECT repeat('q', {bytes}) AS payload"),
                Engine::MySql => format!("SELECT REPEAT('q', {bytes}) AS payload"),
                _ => return Err(Failure::invalid("Speedtest supports PostgreSQL and MySQL")),
            };
            let mut values = vec![];
            for _ in 0..runs {
                let mut r = QueryRequest::new(&query);
                r.policy.max_bytes = bytes + 4096;
                r.policy.timeout_ms = timeout.map(seconds_to_millis).transpose()?;
                let start = Instant::now();
                let out = host.execute(&c, r).await?;
                values.push(json!({"elapsedMs":start.elapsed().as_millis(),"returnedBytes":out.download_bytes}));
            }
            metadata_output(json!({"db":db,"samples":values}), &format)
        }
        Command::Update | Command::InternalKeeper { .. } | Command::InternalNeptune { .. } => {
            unreachable!()
        }
    };
    host.tunnels.close_all().await?;
    result
}
fn resolve_config_path(config: Option<PathBuf>) -> Result<PathBuf> {
    let path = match config {
        Some(path) => path,
        None => home()?.join(".config/quarry/config.toml"),
    };
    Ok(quarry_runtime::absolute(&path, &std::env::current_dir()?)?)
}

/// Initialize the same local runtime used by the standalone CLI and workbench.
pub fn local_host(
    config: Option<PathBuf>,
    workspaces: Vec<PathBuf>,
    launcher_prefix: &[String],
) -> Result<quarry_service::QuarryService> {
    create_host(resolve_config_path(config)?, workspaces, launcher_prefix)
}

fn create_host(
    config: PathBuf,
    workspaces: Vec<PathBuf>,
    launcher_prefix: &[String],
) -> Result<Host> {
    let state = config
        .parent()
        .unwrap_or(Path::new("."))
        .join("rust-runtime");
    Ok(Host::new(config, workspaces, state)
        .with_local_tools(
            std::env::var_os("QUARRY_PSQL").map(PathBuf::from),
            std::env::var_os("QUARRY_PG_DUMP").map(PathBuf::from),
        )
        .with_neptune_launcher(host_launcher(launcher_prefix, "internal-neptune")?)
        .with_keeper_launcher(host_launcher(launcher_prefix, "internal-keeper")?))
}

fn cli_federation(host: &Host, source: Option<Source>) -> Result<Option<Federation>> {
    if source == Some(Source::Local) || std::env::var_os("HOME").is_none() {
        return Ok(None);
    }
    let federation = Federation::new(&host.config_path)?;
    Ok(federation.registry().has_providers()?.then_some(federation))
}

fn target_federation(
    host: &Host,
    source: Option<Source>,
    reference: Option<&str>,
) -> Result<Option<Federation>> {
    let source = source.or_else(|| {
        reference
            .filter(|id| federated_cli::is_local_id(id))
            .map(|_| Source::Local)
    });
    cli_federation(host, source)
}

async fn select_target(
    host: &Host,
    federation: Option<&Federation>,
    db: &str,
    env: Option<&str>,
    source: Option<Source>,
) -> Result<Target> {
    if federation.is_none() && source == Some(Source::Cloud) {
        return Err(Failure::invalid("No cloud database provider is configured"));
    }
    if federation.is_none() && !federated_cli::is_local_id(db) && !federated_cli::is_cloud_id(db) {
        return Ok(Target::local(host.resolve(db, env, None)?));
    }
    let catalog = match federation {
        Some(federation) if source == Some(Source::Cloud) || federated_cli::is_cloud_id(db) => {
            Catalog::cloud_only(federation).await?
        }
        _ => Catalog::new(host, federation).await?,
    };
    Ok(catalog
        .select(&Selector {
            target: db,
            source,
            env,
            workspace: None,
        })?
        .clone())
}

fn validate_cloud_query(options: &QueryOptions) -> Result<()> {
    if options.write {
        return Err(Failure::invalid("Cloud database queries are read-only"));
    }
    if options.no_proxy || options.timeout.is_some() {
        return Err(Failure::invalid(
            "--no-proxy and --timeout apply to local connections",
        ));
    }
    if !(1..=5000).contains(&options.max_rows) {
        return Err(Failure::invalid(
            "Cloud max-rows must be between 1 and 5000",
        ));
    }
    Ok(())
}

async fn execute_target(
    host: &Host,
    federation: Option<&Federation>,
    target: &Target,
    sql: String,
    parameters: BTreeMap<String, String>,
    options: &QueryOptions,
) -> Result<()> {
    let request_id = uuid::Uuid::new_v4().to_string();
    let Target::Local { record: c, .. } = target else {
        validate_cloud_query(options)?;
        let rendered = quarry_core::policy::substitute(target.engine()?, &sql, &parameters)?;
        let result = target
            .execute(
                host,
                federation,
                &rendered,
                options.max_rows,
                options.offset,
                &request_id,
            )
            .await;
        record_query(host, target, &sql, &request_id).await;
        let result = result?;
        let rows = result["rows"]
            .as_array()
            .ok_or_else(|| Failure::invalid("Invalid cloud query rows"))?;
        let columns = result["columns"]
            .as_array()
            .ok_or_else(|| Failure::invalid("Invalid cloud query columns"))?;
        let names = quarry_core::unique_names(
            columns
                .iter()
                .map(|column| column["name"].as_str().unwrap_or_default()),
        );
        return format_rows(rows, &names, result["truncated"] == true, &options.format);
    };
    if options.write && c.production {
        confirmation("Allow a write to this production connection", options.yes)?;
    }
    let mut r = QueryRequest::new(&sql);
    r.parameters = parameters;
    r.policy.allow_write = options.write;
    r.policy.confirm_production = options.yes || options.write && c.production;
    r.policy.max_rows = Some(options.max_rows).filter(|n| *n > 0);
    r.policy.offset = options.offset;
    r.policy.timeout_ms = execution_timeout(c, options.timeout)?;
    let cancellation = r.cancellation.clone();
    let interrupt = tokio::spawn(async move {
        let _ = tokio::signal::ctrl_c().await;
        cancellation.cancel();
    });
    let outcome = host
        .execute_with_proxy(c, r, options.no_proxy.then_some(false))
        .await;
    interrupt.abort();
    record_query(host, target, &sql, &request_id).await;
    format_result(&outcome?, &options.format)
}

async fn record_query(host: &Host, target: &Target, sql: &str, request_id: &str) {
    if target
        .record_history(&http::NativeHistory::for_host(host), sql, request_id)
        .await
        .is_err()
    {
        eprintln!("Query attempted, but local Quarry history could not be saved");
    }
}
fn format_result(out: &QueryOutcome, format: &str) -> Result<()> {
    let rows = out.rows_json();
    let names = quarry_core::unique_names(out.columns.iter().map(|c| c.name.as_str()));
    format_rows(&rows, &names, out.truncated, format)
}

fn format_rows(rows: &[Value], names: &[String], truncated: bool, format: &str) -> Result<()> {
    match format {
        "json" => output(json!(rows))?,
        "ndjson" => {
            for row in rows {
                output(row.clone())?;
            }
        }
        "csv" => {
            let mut writer = csv::Writer::from_writer(io::stdout());
            writer
                .write_record(names)
                .map_err(|e| Failure::invalid(e.to_string()))?;
            for row in rows {
                writer
                    .write_record(names.iter().map(|n| match &row[n] {
                        Value::Null => String::new(),
                        Value::String(s) => s.clone(),
                        v => v.to_string(),
                    }))
                    .map_err(|e| Failure::invalid(e.to_string()))?;
            }
            writer.flush()?;
        }
        "table" => {
            println!("{}", names.join(" | "));
            for row in rows {
                println!(
                    "{}",
                    names
                        .iter()
                        .map(|n| match &row[n] {
                            Value::String(s) => s.clone(),
                            v => v.to_string(),
                        })
                        .collect::<Vec<_>>()
                        .join(" | ")
                );
            }
        }
        _ => return Err(Failure::invalid("Unknown output format")),
    };
    if truncated {
        eprintln!("Result truncated by execution limits");
    }
    Ok(())
}
async fn connections(host: &Host, action: Option<ConnectionCommand>, format: &str) -> Result<()> {
    match action {
        None | Some(ConnectionCommand::List { .. }) => {
            let format = match &action {
                Some(ConnectionCommand::List {
                    format: Some(inner),
                }) => inner,
                _ => format,
            };
            let federation = cli_federation(host, None)?;
            let catalog = Catalog::new(host, federation.as_ref()).await?;
            metadata_output(catalog.connections_json(host).await?, format)
        }
        Some(ConnectionCommand::Test { key, env, source }) => {
            let federation = target_federation(host, source, Some(&key))?;
            let target =
                select_target(host, federation.as_ref(), &key, env.as_deref(), source).await?;
            output(target.health(host, federation.as_ref(), None).await?)
        }
        Some(ConnectionCommand::Candidates) => {
            let federation = Federation::new(&host.config_path)?;
            let catalog = federation.catalog().await?;
            let candidates =
                federation::association_candidates(&catalog, &host.catalog()?.connections()?);
            metadata_output(
                json!(
                    candidates
                        .into_iter()
                        .map(|candidate| json!({
                            "resource_id":candidate.resource_id,
                            "local_id":candidate.local.id,
                            "local":candidate.local,
                            "evidence":candidate.evidence,
                        }))
                        .collect::<Vec<_>>()
                ),
                format,
            )
        }
        Some(ConnectionCommand::Reconcile { project, apply }) => {
            let federation = Federation::new(&host.config_path)?;
            output(reconcile_connections(host, &federation, project.as_deref(), apply).await?)
        }
        Some(ConnectionCommand::Link { targets, yes }) => {
            let (pairs, unmatched) = targets.as_chunks::<2>();
            if !unmatched.is_empty() {
                return Err(Failure::invalid("Pass one local ID after each cloud ID"));
            }
            let federation = Federation::new(&host.config_path)?;
            let catalog = federation.catalog().await?;
            let mut entries = Vec::new();
            let mut linked = Vec::new();
            let mut selected = std::collections::HashSet::new();
            for [cloud, local] in pairs {
                if !selected.insert(cloud) {
                    return Err(Failure::invalid(
                        "A cloud target can only have one association",
                    ));
                }
                let resource = catalog
                    .resources
                    .iter()
                    .find(|resource| {
                        resource.id == *cloud && resource.status == federation::Status::Connected
                    })
                    .ok_or_else(|| {
                        Failure::invalid("Select an available cloud resource by exact ID")
                    })?;
                let record = federated_cli::resolve_local_target(host, local)?;
                if record.spec()?.engine != resource.engine {
                    return Err(Failure::invalid(
                        "Connections must use the same database engine",
                    ));
                }
                entries.push((
                    cloud.clone(),
                    federation::LocalTarget {
                        id: local.clone(),
                        workspace: record.workspace,
                        key: record.key,
                    },
                ));
                linked.push(json!({"resourceId":cloud,"localId":local,"linked":true}));
            }
            if entries.len() > 1 {
                confirmation(
                    &format!(
                        "Link {} cloud resources to the selected local connections",
                        entries.len()
                    ),
                    yes,
                )?;
            }
            federation.registry().associate_many(&entries)?;
            if linked.len() == 1 {
                output(linked.remove(0))
            } else {
                output(json!({"associations":linked}))
            }
        }
        Some(ConnectionCommand::Unlink { cloud }) => {
            let federation = Federation::new(&host.config_path)?;
            output(
                json!({"resourceId":cloud,"unlinked":federation.registry().disassociate(&cloud)?}),
            )
        }
        Some(ConnectionCommand::Remove { key, yes }) => {
            confirmation("Remove connection", yes)?;
            output(json!({"removed":host.catalog()?.remove_connection(&primary(host)?,&key)?}))
        }
        Some(ConnectionCommand::Add(fields)) => put_connection(host, fields, false).await,
        Some(ConnectionCommand::Set(fields)) => put_connection(host, fields, true).await,
    }
}

/// Verify PostgreSQL routes through their existing local and provider credentials.
pub async fn reconcile_connections(
    host: &quarry_service::QuarryService,
    federation: &Federation,
    project: Option<&str>,
    apply: bool,
) -> quarry_service::Result<Value> {
    let catalog = federation.catalog().await?;
    let locals = host.catalog()?.connections()?;
    let discovery =
        verified_associations::discover(host, federation, &catalog, &locals, project).await;
    let mut applied = Vec::new();
    if apply {
        let entries: Vec<_> = discovery
            .candidates
            .iter()
            .filter(|candidate| !candidate.already_linked)
            .map(|candidate| (candidate.resource_id.clone(), candidate.local.clone()))
            .collect();
        if !entries.is_empty() {
            let latest = federation.catalog().await?;
            for (cloud_id, local) in &entries {
                let unchanged = catalog.resources.iter().any(|previous| {
                    previous.id == *cloud_id
                        && latest.resources.iter().any(|resource| {
                            resource.id == *cloud_id
                                && resource.status == federation::Status::Connected
                                && resource.provider == previous.provider
                                && resource.upstream_id == previous.upstream_id
                                && resource.project == previous.project
                                && resource.db == previous.db
                                && resource.env == previous.env
                                && resource.engine == previous.engine
                                && resource.production == previous.production
                        })
                });
                if !unchanged || federated_cli::resolve_local_target(host, &local.id).is_err() {
                    return Err(Failure::invalid(
                        "A database route changed during verification; run reconcile again",
                    ));
                }
            }
            applied = federation.registry().associate_missing(&entries)?;
        }
    }
    Ok(json!({
        "cloudStatus": catalog.status,
        "candidates": discovery.candidates,
        "skipped": discovery.skipped,
        "unavailableLocalCount": discovery.unavailable_local_count,
        "applied": applied,
    }))
}

async fn put_connection(host: &Host, f: ConnectionFields, overwrite: bool) -> Result<()> {
    let env_explicit = f.env.is_some();
    let catalog = host.catalog()?;
    let workspace = primary(host)?;
    let old = catalog
        .connections()?
        .into_iter()
        .find(|c| c.key == f.key && c.workspace == workspace);
    if old.is_some() && !overwrite {
        return Err(Failure::invalid("Connection already exists"));
    }
    let mut value = old
        .as_ref()
        .map(serde_json::to_value)
        .transpose()?
        .unwrap_or(json!({"key":f.key,"url":"","workspace":workspace}));
    for (key, v) in [
        ("url", f.url),
        ("engine", f.engine),
        ("db", f.db),
        ("env", f.env),
        ("group", f.group),
        ("notes", f.notes),
        ("region", f.region),
        ("ssh_host", f.ssh_host),
        ("ssh_user", f.ssh_user),
        ("ssh_key", f.ssh_key),
    ] {
        if let Some(v) = v {
            value[key] = json!(v);
        }
    }
    if f.production || f.no_production {
        value["production"] = json!(f.production);
    }
    if let Some(port) = f.ssh_port {
        value["ssh_port"] = json!(port);
    }
    if let Some(timeout) = f.timeout {
        value["timeout"] = json!(timeout);
    }
    let mut connection: quarry_runtime::ConnectionRecord = serde_json::from_value(value)?;
    connection.env_explicit = env_explicit || old.as_ref().is_some_and(|c| c.env_explicit);
    if connection.url.is_empty() {
        return Err(Failure::invalid("A connection URL is required"));
    }
    if !f.force
        && let Ok(url) = url::Url::parse(&connection.url)
        && url
            .host_str()
            .is_some_and(|value| !matches!(value, "localhost" | "127.0.0.1" | "::1"))
    {
        for other in catalog.connections()? {
            let other_url = url::Url::parse(&other.url).ok();
            if other.key != connection.key
                && other_url.as_ref().is_some_and(|other| {
                    other.host_str() == url.host_str()
                        && other.port_or_known_default() == url.port_or_known_default()
                        && other.path() != url.path()
                })
            {
                return Err(Failure::invalid(
                    "Shared host/port connection requires --force to acknowledge",
                ));
            }
        }
    }
    if !f.no_test {
        host.metadata(
            &connection,
            MetadataOperation::Ping,
            None,
            CancellationToken::new(),
        )
        .await?;
    }
    catalog.put_connection(&workspace, &connection, overwrite)?;
    output(json!({"saved":connection.key}))
}
async fn local(host: &Host, action: LocalCommand) -> Result<()> {
    if let LocalCommand::Connect {
        server,
        ticket,
        port,
    } = &action
    {
        return tunnel::connect(server, ticket, *port).await;
    }
    let manager = host.local()?;
    match action {
        LocalCommand::Status { engine, format } => {
            metadata_output(manager.status(engine.as_deref()).await?, &format)
        }
        LocalCommand::Up {
            db,
            engine,
            image,
            port,
            fixture,
        } => {
            if engine.as_deref() == Some("sqlite") {
                return Err(Failure::invalid(
                    "SQLite uses an existing local file and does not need Docker; add a sqlite:/// connection instead",
                ));
            }
            if let Some(db) = db {
                let catalog = host.catalog()?;
                if db.ends_with("_local")
                    && !catalog.connections()?.iter().any(|record| record.key == db)
                {
                    return Err(Failure::invalid(
                        "Names ending in _local are reserved for generated local connections",
                    ));
                }
                let source = local_source(&catalog, &db)?;
                let selected = if let Some(source) = &source {
                    source.spec()?.engine.as_str().to_owned()
                } else {
                    engine
                        .as_deref()
                        .filter(|engine| *engine != "all")
                        .unwrap_or("postgres")
                        .to_owned()
                };
                if !["postgres", "redis", "neptune"].contains(&selected.as_str()) {
                    return Err(Failure::invalid(
                        "This database engine has no managed local service",
                    ));
                }
                if engine
                    .as_deref()
                    .is_some_and(|engine| engine != "all" && engine != selected)
                {
                    return Err(Failure::invalid(
                        "The selected engine does not match the source database",
                    ));
                }
                if fixture.is_some() && selected != "neptune" {
                    return Err(Failure::invalid(
                        "--fixture requires a single local Neptune target",
                    ));
                }
                let image = match image {
                    Some(image) => Some(image),
                    None => source
                        .as_ref()
                        .map(|source| manager.stored_image(&catalog, source))
                        .transpose()?
                        .flatten(),
                };
                let status = if selected == "neptune" {
                    manager.up_neptune(port, fixture.as_deref()).await?
                } else {
                    manager.up(&selected, image.as_deref(), port).await?
                };
                let record = if let Some(source) = source {
                    manager
                        .register(&catalog, &source.key, Some(&source.env))
                        .await?
                } else {
                    manager
                        .register_new(&catalog, &primary(host)?, &db, &selected)
                        .await?
                };
                output(
                    json!({"key":record.key,"db":record.logical_db(),"env":record.display_env(),"workspace":record.workspace,"engine":selected,"status":status}),
                )
            } else {
                let engines = match engine.as_deref() {
                    None | Some("all") => vec!["postgres", "redis", "neptune"],
                    Some(engine) => vec![engine],
                };
                if port.is_some() && engines.len() != 1 {
                    return Err(Failure::invalid(
                        "--port requires a single engine or database key",
                    ));
                }
                if fixture.is_some() && engines != vec!["neptune"] {
                    return Err(Failure::invalid(
                        "--fixture requires --engine neptune or a database key",
                    ));
                }
                let mut results = Vec::new();
                for engine in engines {
                    results.push(if engine == "neptune" {
                        manager.up_neptune(port, fixture.as_deref()).await?
                    } else {
                        manager.up(engine, image.as_deref(), port).await?
                    });
                }
                output(if results.len() == 1 {
                    results.remove(0)
                } else {
                    json!(results)
                })
            }
        }
        LocalCommand::Down { engine, purge, yes } => {
            if purge {
                confirmation("Purge managed local database data", yes)?;
            }
            output(manager.down(&engine, purge).await?)
        }
        LocalCommand::Sync { db, from } => output(host.local_sync(&db, &from).await?),
        LocalCommand::NeptuneCalls { after, format } => {
            metadata_output(manager.neptune_calls(after).await?, &format)
        }
        LocalCommand::Connect { .. } => unreachable!("handled before local manager setup"),
    }
}
async fn ai(host: &Host, command: AiCommand) -> Result<()> {
    let a = assistant(host)?;
    match command {
        AiCommand::Show => output(a.config().details()?),
        AiCommand::Language { language } => output(if let Some(l) = language {
            a.config().set_reply_language(&l)?
        } else {
            a.config().preferences()?
        }),
        AiCommand::Configure(options) => {
            let mut prompt = TerminalModelPrompt;
            let result = configure_model(a.config(), options, &mut prompt)?;
            output(result)
        }
        AiCommand::Explain {
            db,
            env,
            sql,
            file,
            format,
        } => {
            generate(
                a,
                db,
                env,
                read_sql(sql, file)?,
                "请用中文解读这段 SQL。".into(),
                true,
                &format,
            )
            .await
        }
        AiCommand::Sql {
            db,
            env,
            sql,
            file,
            prompt,
            format,
        } => {
            let sql = if sql.is_none() && file.is_none() {
                String::new()
            } else {
                read_sql(sql, file)?
            };
            generate(a, db, env, sql, prompt, false, &format).await
        }
    }
}
struct AbortTask(tokio::task::JoinHandle<()>);
impl Drop for AbortTask {
    fn drop(&mut self) {
        self.0.abort();
    }
}
async fn generate(
    a: Arc<quarry_assistant::AssistantService>,
    db: String,
    env: Option<String>,
    sql: String,
    prompt: String,
    explain: bool,
    format: &str,
) -> Result<()> {
    use futures_util::StreamExt;
    a.config().load()?;
    if sql.chars().count() > quarry_assistant::MAX_SQL
        || sql.contains('\0')
        || prompt.chars().count() > quarry_assistant::MAX_PROMPT
        || prompt.trim().is_empty()
        || prompt.contains('\0')
    {
        return Err(Failure::invalid(
            "Assistant SQL or message exceeds the supported input limits",
        ));
    }
    if explain && sql.trim().is_empty() {
        return Err(Failure::invalid(
            "Provide SQL with --sql or --file (use --file - for stdin)",
        ));
    }
    if !(if explain {
        ["text", "json"].contains(&format)
    } else {
        ["sql", "json"].contains(&format)
    }) {
        return Err(Failure::invalid("Unsupported assistant output format"));
    }
    let conversation = a
        .create_conversation(json!({"db":db,"env":env,"sql":sql}))
        .await?;
    let mut stream=a.start_chat(json!({"conversationId":conversation["id"],"revision":conversation["revision"],"sql":sql,"prompt":prompt,"explain":explain,"requestId":uuid::Uuid::new_v4().to_string()}),false).await?;
    let shutdown = a.clone();
    let _interrupt = AbortTask(tokio::spawn(async move {
        let _ = tokio::signal::ctrl_c().await;
        shutdown.close();
    }));
    let mut text = String::new();
    let mut proposal = None;
    let mut completed = None;
    while let Some(event) = stream.next().await {
        match event["type"].as_str() {
            Some("text_delta") => {
                let delta = event["delta"].as_str().unwrap_or("");
                text += delta;
                if explain && format == "text" {
                    print!("{delta}");
                    io::stdout().flush()?;
                }
            }
            Some("sql_proposal") => proposal = event["sql"].as_str().map(String::from),
            Some("error") => {
                if explain && format == "text" && !text.is_empty() {
                    println!();
                }
                return Err(Failure::invalid(
                    event["error"].as_str().unwrap_or("Assistant failed"),
                ));
            }
            Some("done") => completed = Some(event["conversation"].clone()),
            _ => {}
        }
    }
    if completed.is_none() {
        return Err(Failure::invalid("Assistant response did not complete"));
    }
    if format == "json" {
        output(
            json!({"conversationId":conversation["id"],"text":text,"sql":proposal,"conversation":completed}),
        )
    } else if explain {
        println!();
        Ok(())
    } else {
        println!(
            "{}",
            proposal.ok_or_else(|| Failure::invalid("The assistant did not propose SQL"))?
        );
        Ok(())
    }
}

fn open_gui(url: &str) {
    let _ = std::process::Command::new(if cfg!(target_os = "macos") {
        "open"
    } else {
        "xdg-open"
    })
    .arg(url)
    .spawn();
}

async fn keepalive(host: &Host, enabled: Option<bool>) -> Result<Value> {
    let workspace = primary(host)?;
    let mut config = RuntimeConfig::load(&host.config_path)?;
    let manager =
        quarry_runtime::KeeperManager::new(host.config_path.clone(), host.state_dir.clone());
    if let Some(enabled) = enabled {
        config = config.set_toggle("tunnel_keep_alive_workspaces", &workspace, enabled)?;
        if enabled {
            if let Some(launcher) = &host.keeper_launcher {
                manager.start_with_launcher(&workspace, launcher).await?;
            } else {
                manager.start(&workspace, &std::env::current_exe()?).await?;
            }
        } else {
            manager.stop(&workspace).await?;
        }
    }
    let snapshot = manager.status(&workspace)?;
    Ok(
        json!({"workspace":workspace,"enabled":config.enabled("keep_alive",&workspace),"reconnect":config.enabled("reconnect",&workspace),"keeper":{"running":snapshot["running"],"pid":snapshot.get("pid").cloned().unwrap_or(Value::Null)},"tunnels":snapshot["status"]["tunnels"],"updatedAt":std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs()}),
    )
}

fn locale_language(value: &str) -> Option<&'static str> {
    let first = value
        .trim()
        .split(['-', '_', '.', '@'])
        .next()
        .unwrap_or("");
    if first.is_empty() || first.eq_ignore_ascii_case("C") || first.eq_ignore_ascii_case("POSIX") {
        None
    } else {
        Some(quarry_assistant::resolve_language(first))
    }
}
#[cfg(any(target_os = "macos", test))]
fn parse_macos_languages(value: &str) -> Option<&'static str> {
    // `defaults read -g AppleLanguages` is an OpenStep array of locale tags.
    // Inspect only the preferred first member; never read all OS preferences.
    let first = value
        .trim()
        .strip_prefix('(')?
        .split(',')
        .next()?
        .trim()
        .trim_end_matches(')')
        .trim()
        .trim_matches('"');
    locale_language(first)
}
#[cfg(target_os = "macos")]
fn macos_preference(key: &str) -> Option<String> {
    use std::process::{Command, Stdio};
    use std::time::Duration;
    let mut child = Command::new("/usr/bin/defaults")
        .args(["read", "-g", key])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                if !status.success() {
                    return None;
                }
                let mut output = String::new();
                child
                    .stdout
                    .take()?
                    .take(8193)
                    .read_to_string(&mut output)
                    .ok()?;
                return (output.len() <= 8192).then_some(output);
            }
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
}
fn system_reply_language() -> String {
    for name in ["LC_ALL", "LC_MESSAGES", "LANG"] {
        if let Ok(value) = std::env::var(name)
            && let Some(language) = locale_language(&value)
        {
            return language.into();
        }
    }
    #[cfg(target_os = "macos")]
    {
        if let Some(language) = macos_preference("AppleLanguages")
            .as_deref()
            .and_then(parse_macos_languages)
        {
            return language.into();
        }
        if let Some(language) = macos_preference("AppleLocale")
            .as_deref()
            .and_then(locale_language)
        {
            return language.into();
        }
    }
    "en".into()
}
trait ModelPrompt {
    fn interactive(&self) -> bool;
    fn text(&mut self, label: &str, default: &str) -> Result<String>;
    fn secret(&mut self) -> Result<String>;
    fn stdin_secret(&mut self) -> Result<String>;
}
struct TerminalModelPrompt;
impl ModelPrompt for TerminalModelPrompt {
    fn interactive(&self) -> bool {
        io::stdin().is_terminal()
    }
    fn text(&mut self, label: &str, default: &str) -> Result<String> {
        if default.is_empty() {
            eprint!("{label}: ");
        } else {
            eprint!("{label} [{default}]: ");
        }
        io::stderr().flush()?;
        let mut line = String::new();
        if io::stdin().read_line(&mut line)? == 0 {
            return Err(Failure::invalid("Model configuration cancelled"));
        }
        let line = line.trim();
        Ok(if line.is_empty() {
            default.into()
        } else {
            line.into()
        })
    }
    fn secret(&mut self) -> Result<String> {
        rpassword::prompt_password("API key (hidden; blank keeps the current key): ")
            .map(|key| key.trim().to_owned())
            .map_err(|_| Failure::invalid("Model configuration cancelled"))
    }
    fn stdin_secret(&mut self) -> Result<String> {
        let mut key = String::new();
        io::stdin().take(8194).read_to_string(&mut key)?;
        if key.len() > 8192 {
            return Err(Failure::invalid("API key is too large"));
        }
        Ok(key.trim().into())
    }
}
fn configure_model(
    manager: &quarry_assistant::ConfigManager,
    options: AiConfigureOptions,
    prompt: &mut dyn ModelPrompt,
) -> Result<Value> {
    use quarry_assistant::Credential;
    let modes = usize::from(options.api_key_env.is_some())
        + usize::from(options.api_key_stdin)
        + usize::from(options.no_key);
    if modes > 1 {
        return Err(Failure::invalid("Choose exactly one API key mode"));
    }
    let interactive = options.base_url.is_none() || options.model.is_none() || modes == 0;
    if interactive && !prompt.interactive() {
        return Err(Failure::invalid(
            "Interactive configuration needs a terminal. Provide --base-url, --model and a key option; see `qy ai configure --help`",
        ));
    }
    let current = manager.details()?;
    let base = match options.base_url {
        Some(value) => value,
        None => prompt.text("Base URL", current["base_url"].as_str().unwrap_or(""))?,
    };
    let model = match options.model {
        Some(value) => value,
        None => prompt.text("Model", current["model"].as_str().unwrap_or(""))?,
    };
    let credential = if let Some(name) = options.api_key_env {
        Credential::Environment(name)
    } else if options.api_key_stdin {
        Credential::Stored(prompt.stdin_secret()?)
    } else if options.no_key {
        Credential::None
    } else {
        let key = prompt.secret()?;
        if !key.is_empty() {
            Credential::Stored(key)
        } else {
            match current["auth"].as_str() {
                Some("env") => Credential::Environment(
                    current["api_key_env"]
                        .as_str()
                        .unwrap_or("QUARRY_AI_API_KEY")
                        .into(),
                ),
                Some("none") => Credential::None,
                Some("stored") if current["configured"] == true => {
                    Credential::Stored(manager.load()?.api_key)
                }
                _ => {
                    return Err(Failure::invalid(
                        "An API key is required; for an endpoint without authentication use --no-key",
                    ));
                }
            }
        }
    };
    let bifrost = if interactive && !options.bifrost {
        let default = if current["bifrost"] == true { "y" } else { "n" };
        matches!(
            prompt
                .text("Use Bifrost virtual-key headers? (y/n)", default)?
                .to_ascii_lowercase()
                .as_str(),
            "y" | "yes"
        )
    } else {
        options.bifrost
    };
    let thinking = options.thinking.unwrap_or_else(|| {
        if interactive {
            current["thinking"].as_str().unwrap_or("default").into()
        } else {
            "default".into()
        }
    });
    Ok(manager.configure(
        &base,
        &model,
        credential,
        bifrost,
        &thinking,
        options.timeout,
    )?)
}
fn seconds_to_millis(seconds: u64) -> Result<u64> {
    if seconds == 0 {
        return Err(Failure::invalid(
            "Timeout must be a positive integer number of seconds",
        ));
    }
    seconds
        .checked_mul(1000)
        .ok_or_else(|| Failure::invalid("Timeout is too large"))
}
fn execution_timeout(
    record: &quarry_runtime::ConnectionRecord,
    explicit: Option<u64>,
) -> Result<Option<u64>> {
    if let Some(seconds) = explicit {
        return Ok(Some(seconds_to_millis(seconds)?));
    }
    if let Ok(value) = std::env::var("QUARRY_TIMEOUT") {
        // Match the historical policy: an invalid environment override is
        // ignored, rather than cancelling every query with a near-zero limit.
        if let Ok(seconds) = value.parse::<u64>()
            && seconds > 0
        {
            return Ok(Some(seconds_to_millis(seconds)?));
        }
    }
    match record.extra.get("timeout") {
        Some(value) => {
            let seconds = value.as_integer().filter(|n| *n > 0).ok_or_else(|| {
                Failure::invalid("Connection timeout must be positive integer seconds")
            })?;
            Ok(Some(seconds_to_millis(seconds as u64)?))
        }
        None => Ok(None),
    }
}

fn local_source(
    catalog: &quarry_runtime::WorkspaceCatalog,
    name: &str,
) -> Result<Option<quarry_runtime::ConnectionRecord>> {
    let connections = catalog.connections()?;
    let exact = connections
        .iter()
        .filter(|record| record.key == name)
        .collect::<Vec<_>>();
    if exact.len() > 1 {
        return Err(Failure::invalid(
            "Ambiguous database key; select a workspace",
        ));
    }
    if let Some(record) = exact.first() {
        return Ok(Some((*record).clone()));
    }
    let mut matches = connections
        .into_iter()
        .filter(|record| record.logical_db() == name)
        .collect::<Vec<_>>();
    let groups = matches
        .iter()
        .filter_map(|record| record.group.as_deref())
        .filter(|group| !group.is_empty())
        .collect::<std::collections::BTreeSet<_>>();
    let environments = matches
        .iter()
        .map(|record| record.display_env())
        .collect::<std::collections::BTreeSet<_>>();
    if groups.len() > 1 || environments.len() != matches.len() {
        return Err(Failure::invalid(
            "Ambiguous logical database; select a workspace",
        ));
    }
    matches.sort_by(|a, b| (a.env != "dev", &a.env).cmp(&(b.env != "dev", &b.env)));
    Ok(matches.into_iter().next())
}
fn parse_query_parameter(value: &str) -> Result<QueryParameter> {
    let mut parts = value.splitn(3, ':');
    let name = parts.next().unwrap_or("").trim();
    let kind = parts.next().unwrap_or("text").trim();
    let qualifier = parts.next().unwrap_or("").trim();
    quarry_runtime::validate_name(name)?;
    if kind.is_empty() || kind.len() > 100 || kind.chars().any(char::is_control) {
        return Err(Failure::invalid("Invalid query parameter type"));
    }
    let (required, default) = if qualifier == "required" {
        (true, None)
    } else if let Some(value) = qualifier.strip_prefix("default=") {
        (false, Some(value.into()))
    } else if qualifier.is_empty() {
        (false, None)
    } else {
        return Err(Failure::invalid(
            "Unknown query parameter qualifier; use required or default=...",
        ));
    };
    Ok(QueryParameter {
        name: name.into(),
        r#type: kind.into(),
        required,
        default,
    })
}

pub fn failure_exit_code(error: &Failure) -> i32 {
    match error.code.as_str() {
        "connection" => 2,
        "execution" | "timeout" | "cancelled" | "budget" => 3,
        "safety" => 8,
        _ => 1,
    }
}

fn host_launcher(prefix: &[String], command: &str) -> Result<quarry_runtime::DaemonLauncher> {
    let mut launcher = quarry_runtime::DaemonLauncher::new(std::env::current_exe()?);
    for argument in prefix {
        launcher = launcher.arg(argument);
    }
    Ok(launcher.arg(command))
}
/// Embedded workbench assets. Host servers can serve them behind their own IAM
/// gateway without enabling the standalone loopback-only HTTP adapter.
pub fn web_asset(path: &str) -> Option<(&'static [u8], String)> {
    http::web_asset(path)
}

#[cfg(test)]
mod cli_behavior_tests {
    use super::*;
    use std::collections::VecDeque;

    #[test]
    fn query_options_after_positional_parameters() {
        for (command, name) in [("run", "session-snapshots"), ("exec", "matrix_runtime")] {
            let cli = Cli::try_parse_from([
                "qy",
                command,
                name,
                "session_id=example",
                "limit=1",
                "--format",
                "ndjson",
                "--env",
                "dev",
            ])
            .unwrap();
            let (params, options) = match cli.command {
                Command::Run {
                    params, options, ..
                }
                | Command::Exec {
                    params, options, ..
                } => (params, options),
                _ => unreachable!(),
            };
            assert_eq!(params, ["session_id=example", "limit=1"]);
            assert_eq!(options.format, "ndjson");
            assert_eq!(options.env.as_deref(), Some("dev"));
        }
    }

    struct Prompt {
        tty: bool,
        answers: VecDeque<String>,
        hidden: String,
        labels: Vec<String>,
    }
    impl ModelPrompt for Prompt {
        fn interactive(&self) -> bool {
            self.tty
        }
        fn text(&mut self, label: &str, default: &str) -> Result<String> {
            self.labels.push(format!("{label} [{default}]"));
            let answer = self.answers.pop_front().expect("unexpected prompt");
            Ok(if answer.is_empty() {
                default.into()
            } else {
                answer
            })
        }
        fn secret(&mut self) -> Result<String> {
            self.labels.push("hidden-key".into());
            Ok(self.hidden.clone())
        }
        fn stdin_secret(&mut self) -> Result<String> {
            Ok(self.hidden.clone())
        }
    }
    fn options() -> AiConfigureOptions {
        AiConfigureOptions {
            base_url: None,
            model: None,
            api_key_env: None,
            api_key_stdin: false,
            no_key: false,
            bifrost: false,
            thinking: None,
            timeout: 30,
        }
    }
    #[test]
    fn interactive_model_configuration_uses_defaults_and_keeps_stored_secret_private() {
        let dir = tempfile::tempdir().unwrap();
        let config = quarry_assistant::ConfigManager::new(
            dir.path().join("config.toml"),
            HashMap::new(),
            "en",
        );
        config
            .configure(
                "https://model.example/v1",
                "prior",
                quarry_assistant::Credential::Stored("kept-secret".into()),
                true,
                "disabled",
                30,
            )
            .unwrap();
        let mut prompt = Prompt {
            tty: true,
            answers: VecDeque::from(["".into(), "updated-model".into(), "".into()]),
            hidden: "".into(),
            labels: vec![],
        };
        let result = configure_model(&config, options(), &mut prompt).unwrap();
        assert_eq!(result["configured"], true);
        assert_eq!(config.load().unwrap().model, "updated-model");
        assert_eq!(config.load().unwrap().api_key, "kept-secret");
        assert!(config.load().unwrap().bifrost);
        assert_eq!(config.load().unwrap().thinking, "disabled");
        assert!(!result.to_string().contains("kept-secret"));
        assert!(!prompt.labels.join("\n").contains("kept-secret"));
        assert!(prompt.answers.is_empty());
    }
    #[test]
    fn model_configuration_refuses_missing_noninteractive_input_and_conflicting_key_modes() {
        let dir = tempfile::tempdir().unwrap();
        let config = quarry_assistant::ConfigManager::new(
            dir.path().join("config.toml"),
            HashMap::new(),
            "en",
        );
        let mut prompt = Prompt {
            tty: false,
            answers: VecDeque::new(),
            hidden: "secret".into(),
            labels: vec![],
        };
        assert!(
            configure_model(&config, options(), &mut prompt)
                .unwrap_err()
                .message
                .contains("terminal")
        );
        let mut conflict = options();
        conflict.api_key_stdin = true;
        conflict.no_key = true;
        assert!(configure_model(&config, conflict, &mut prompt).is_err());
        assert!(!config.config_path.exists());
        assert!(!config.credentials_path.exists());
        for flags in [
            vec!["--no-key", "--api-key-stdin"],
            vec!["--no-key", "--api-key-env", "SECRET"],
            vec!["--api-key-stdin", "--api-key-env", "SECRET"],
        ] {
            let mut argv = vec!["qy", "ai", "configure"];
            argv.extend(flags);
            assert!(Cli::try_parse_from(argv).is_err());
        }
        assert!(Cli::try_parse_from(["qy", "ai", "configure"]).is_ok());
        assert!(Cli::try_parse_from(["qy", "local", "down", "--purge", "--yes"]).is_ok());
    }
    #[tokio::test]
    async fn unconfigured_ai_generation_does_not_create_history() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history.sqlite3");
        let host = Host::new(
            dir.path().join("config.toml"),
            vec![],
            dir.path().join("state"),
        );
        let service = Arc::new(quarry_assistant::AssistantService::new(
            quarry_assistant::ConversationStore::new(&path),
            quarry_assistant::ConfigManager::new(
                dir.path().join("config.toml"),
                HashMap::new(),
                "en",
            ),
            Arc::new(host),
        ));
        assert!(
            generate(
                service,
                "not-a-real-db".into(),
                None,
                "SELECT 1".into(),
                "Explain".into(),
                true,
                "json"
            )
            .await
            .unwrap_err()
            .message
            .contains("not configured")
        );
        assert!(!path.exists());
    }
    #[test]
    fn locale_precedence_components_and_parameter_qualifiers_are_preserved() {
        assert_eq!(locale_language("C.UTF-8"), None);
        assert_eq!(locale_language("POSIX"), None);
        assert_eq!(locale_language("zh_CN.UTF-8"), Some("zh"));
        assert_eq!(locale_language("en_US.UTF-8"), Some("en"));
        assert_eq!(
            parse_macos_languages("(\n    \"zh-Hans-CN\",\n    \"en-CN\"\n)\n"),
            Some("zh")
        );
        assert_eq!(parse_macos_languages("(en-US)"), Some("en"));
        let required = parse_query_parameter("account:int:required").unwrap();
        assert!(required.required);
        assert_eq!(required.r#type, "int");
        let default = parse_query_parameter("since:text:default=2026-01-01T09:00:00").unwrap();
        assert!(!default.required);
        assert_eq!(default.default.as_deref(), Some("2026-01-01T09:00:00"));
        assert!(!parse_query_parameter("label").unwrap().required);
        assert!(parse_query_parameter("label:text:unknown").is_err());
        assert!(seconds_to_millis(0).is_err());
        assert!(seconds_to_millis(u64::MAX).is_err());
    }
    #[test]
    fn metadata_formats_render_readable_tables_and_complete_sql() {
        let connections = json!({"groups":[{"group":"Project","ws":"~/workspace","items":[{"db":"notes","envs":[{"env":"local","engine":"sqlite","key":"notes_local","region":null,"ssh":false,"proxied":false,"production":false}]}]}]});
        let text = metadata_text(&connections);
        assert!(text.contains("Project (~/workspace)"));
        assert!(text.contains("database"));
        assert!(text.contains("notes_local"));
        assert!(!text.trim_start().starts_with('{'));
        let schema = metadata_text(
            &json!({"rows":[{"column_name":"note","data_type":"TEXT","is_nullable":"YES"}],"primary_key":["id"],"foreign_keys":[]}),
        );
        assert!(schema.contains("column_name"));
        assert!(schema.contains("TEXT"));
        assert!(schema.contains("Primary key: id"));
        let query = json!({"name":"daily","db":"notes","params":[{"name":"since","type":"text","required":true}],"sql":"SELECT id,\n  note FROM entries"});
        assert!(metadata_text(&query).contains("SQL:\nSELECT id,\n  note FROM entries\n"));
        assert!(!metadata_text(&json!([query])).contains("SELECT"));
        for args in [
            vec!["connections", "--format", "table"],
            vec!["connections", "list", "--format", "text"],
            vec!["schema", "notes", "entries", "--format", "text"],
            vec!["describe-table", "notes", "entries", "--format", "text"],
            vec!["list", "--format", "text"],
            vec!["workspace", "list", "--format", "text"],
            vec!["proxy", "status", "--format", "text"],
            vec!["status", "--format", "text"],
        ] {
            let mut argv = vec!["qy"];
            argv.extend(args);
            assert!(Cli::try_parse_from(argv).is_ok());
        }
        assert!(Cli::try_parse_from(["qy", "list", "--format", "invalid"]).is_err());
    }
}
