use argon2::{Argon2, PasswordHasher, password_hash::SaltString};
use clap::{Parser, Subcommand};
use std::{io::Read, path::PathBuf};
#[derive(Parser)]
#[command(
    version,
    about = "Quarry hosted workbench: one server, configuration and no build-time platform adapter"
)]
struct Args {
    #[arg(
        long,
        env = "QUARRY_HOSTED_CONFIG",
        default_value = "/etc/quarry/hosted.json"
    )]
    config: PathBuf,
    #[command(subcommand)]
    command: Option<Command>,
}
#[derive(Subcommand)]
enum Command {
    /// Print an Argon2id password hash for administrator configuration.
    HashPassword {
        #[arg(long)]
        stdin: bool,
    },
    /// Validate configuration and connection resource resolution without listening.
    Check,
    /// Probe the configured listener for Docker health checks.
    Healthcheck,
}
#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("Quarry server failed: {error}");
        std::process::exit(1);
    }
}
async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();
    if let Some(Command::HashPassword { stdin }) = args.command {
        let password = if stdin {
            let mut value = String::new();
            std::io::stdin().take(4098).read_to_string(&mut value)?;
            value.trim_end_matches(['\r', '\n']).to_owned()
        } else {
            rpassword::prompt_password("Administrator password: ")?
        };
        if password.len() < 12 || password.len() > 4096 {
            return Err("Administrator password must be 12–4096 bytes".into());
        }
        let salt = SaltString::generate(&mut rand_core::OsRng);
        let hash = Argon2::default()
            .hash_password(password.as_bytes(), &salt)
            .map_err(|_| "Cannot hash password")?;
        println!("{hash}");
        return Ok(());
    }
    if matches!(args.command, Some(Command::Healthcheck)) {
        // Health checks must not materialize credentials or mutate runtime state.
        let raw = std::fs::read(&args.config)?;
        if raw.len() > 1024 * 1024 {
            return Err("Hosted configuration exceeds 1 MiB".into());
        }
        let config: serde_json::Value = serde_json::from_slice(&raw)?;
        let listener: std::net::SocketAddr = config["listen"]
            .as_str()
            .unwrap_or("0.0.0.0:8080")
            .parse()
            .map_err(|_| "Invalid configured listener")?;
        let ip = if listener.ip().is_unspecified() {
            if listener.is_ipv6() {
                std::net::IpAddr::V6(std::net::Ipv6Addr::LOCALHOST)
            } else {
                std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)
            }
        } else {
            listener.ip()
        };
        let address = std::net::SocketAddr::new(ip, listener.port());
        let response = reqwest::Client::builder()
            .no_proxy()
            .timeout(std::time::Duration::from_secs(4))
            .build()?
            .get(format!("http://{address}/health"))
            .send()
            .await?;
        if !response.status().is_success() {
            return Err("Hosted health check failed".into());
        }
        return Ok(());
    }
    let config = quarry_server::config::Config::read(&args.config)?;
    let listen = config.listen.clone();
    let version = std::env::var("GIT_SHA")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "dev".into());
    let router = quarry_server::router(config, version)?;
    if matches!(args.command, Some(Command::Check)) {
        println!("Hosted configuration is valid");
        return Ok(());
    }
    let listener = tokio::net::TcpListener::bind(&listen).await?;
    eprintln!(
        "Quarry hosted workbench listening on {}",
        listener.local_addr()?
    );
    axum::serve(listener, router)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}
