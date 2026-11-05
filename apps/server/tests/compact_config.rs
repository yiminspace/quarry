use argon2::{Argon2, PasswordHasher, password_hash::SaltString};
use quarry_server::config::Config;
use serde_json::{Value, json};
use std::path::Path;
fn input(root: &Path) -> Value {
    let salt = SaltString::encode_b64(b"one-file-config-test").unwrap();
    let hash = Argon2::default()
        .hash_password(b"disposable-compact-config", &salt)
        .unwrap()
        .to_string();
    json!({"state_dir":root.join("state"),"auth":{"mode":"builtin","username":"admin","password_hash":hash},"databases":[{"id":"notes","project":"demo","db":"notes","env":"prod","url":"sqlite:///tmp/quarry-config-contract-only.db","readers":["admin"]}]})
}
fn read(root: &Path, value: &Value) -> Result<Config, String> {
    let path = root.join("hosted.json");
    std::fs::write(&path, serde_json::to_vec(value).unwrap()).unwrap();
    Config::read(&path)
}
#[test]
fn one_file_resolves_resources_and_keeps_credentials_private_without_touching_old_workspace() {
    let temp = tempfile::tempdir().unwrap();
    let previous = temp.path().join("existing-workspace");
    std::fs::create_dir(&previous).unwrap();
    std::fs::write(previous.join("keep.txt"), "existing local data").unwrap();
    let config = read(temp.path(), &input(temp.path())).unwrap();
    let service = quarry_service::QuarryService::new(
        config.config_path.clone(),
        config.workspaces.clone(),
        config.state_dir.join("runtime"),
    );
    let resource = &config.resources[0];
    let record = service
        .resolve(
            &resource.connection_key,
            Some(&resource.env),
            resource.workspace.as_deref(),
        )
        .unwrap();
    assert!(record.production);
    assert_eq!(resource.readers, vec!["admin"]);
    assert_eq!(
        std::fs::read_to_string(previous.join("keep.txt")).unwrap(),
        "existing local data"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(config.config_path.parent().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            std::fs::metadata(
                config
                    .config_path
                    .parent()
                    .unwrap()
                    .join("connections.toml")
            )
            .unwrap()
            .permissions()
            .mode()
                & 0o777,
            0o600
        );
    }
}
#[test]
fn ambiguous_configuration_and_duplicate_database_ids_are_rejected_before_materializing() {
    let temp = tempfile::tempdir().unwrap();
    let mut value = input(temp.path());
    value["config_path"] = json!("/unrelated/config.toml");
    assert!(read(temp.path(), &value).is_err());
    assert!(!temp.path().join("state").exists());
    value.as_object_mut().unwrap().remove("config_path");
    let duplicate = value["databases"][0].clone();
    value["databases"].as_array_mut().unwrap().push(duplicate);
    assert!(read(temp.path(), &value).is_err());
    assert!(!temp.path().join("state").exists());
}
#[cfg(unix)]
#[test]
fn generated_symlink_does_not_overwrite_an_existing_file() {
    let temp = tempfile::tempdir().unwrap();
    let value = input(temp.path());
    let generated = temp.path().join("state/hosted-generated");
    std::fs::create_dir_all(&generated).unwrap();
    let target = temp.path().join("keep.toml");
    std::fs::write(&target, "original user configuration").unwrap();
    std::os::unix::fs::symlink(&target, generated.join("connections.toml")).unwrap();
    assert!(read(temp.path(), &value).is_err());
    assert_eq!(
        std::fs::read_to_string(target).unwrap(),
        "original user configuration"
    );
}
