#[tokio::main]
async fn main() {
    if let Err(error) = quarry_cli::run_from(std::env::args_os().collect(), vec![]).await {
        eprintln!(
            "{}",
            serde_json::json!({"error":error.message,"code":error.code})
        );
        std::process::exit(quarry_cli::failure_exit_code(&error));
    }
}
