use reqwest::{Client, Url, redirect::Policy};
use semver::Version;
use serde::Deserialize;
use serde_json::{Value, json};
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

const CURRENT: &str = env!("CARGO_PKG_VERSION");
const RELEASES: &str = "https://github.com/yiminspace/quarry/releases";
const DESCRIPTOR_URL: &str =
    "https://github.com/yiminspace/quarry/releases/latest/download/hosted-release.json";
const IMAGE_PREFIX: &str = "ghcr.io/yiminspace/quarry:sha-";
const CACHE_TTL: Duration = Duration::from_secs(15 * 60);
const MAX_DESCRIPTOR_BYTES: usize = 4096;

pub(crate) struct UpdateCheck {
    client: Option<Client>,
    cache: Mutex<Option<(Instant, Value)>>,
}

impl UpdateCheck {
    pub(crate) fn new() -> Self {
        let client = if std::env::var("QUARRY_UPDATE_CHECK").as_deref() == Ok("0") {
            None
        } else {
            Client::builder()
                .user_agent("quarry-server")
                .timeout(Duration::from_secs(4))
                .redirect(Policy::custom(|attempt| {
                    if attempt.previous().len() <= 4 && is_release_host(attempt.url()) {
                        attempt.follow()
                    } else {
                        attempt.error("Unexpected hosted release redirect")
                    }
                }))
                .build()
                .ok()
        };
        Self {
            client,
            cache: Mutex::new(None),
        }
    }

    pub(crate) async fn status(&self) -> Value {
        let Some(client) = &self.client else {
            return unavailable(CURRENT);
        };
        // Keep a single upstream request in flight, and cache failures too.
        let mut cache = self.cache.lock().await;
        if let Some((updated_at, result)) = &*cache
            && updated_at.elapsed() < CACHE_TTL
        {
            return result.clone();
        }
        let result = fetch_descriptor(client)
            .await
            .and_then(|bytes| release_status(CURRENT, &bytes))
            .unwrap_or_else(|| unavailable(CURRENT));
        *cache = Some((Instant::now(), result.clone()));
        result
    }
}

fn is_release_host(url: &Url) -> bool {
    url.scheme() == "https"
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && match url.host_str() {
            Some("github.com") => url.path().starts_with("/yiminspace/quarry/releases/"),
            Some("release-assets.githubusercontent.com" | "objects.githubusercontent.com") => true,
            _ => false,
        }
}

async fn fetch_descriptor(client: &Client) -> Option<Vec<u8>> {
    let mut response = client
        .get(DESCRIPTOR_URL)
        .send()
        .await
        .ok()?
        .error_for_status()
        .ok()?;
    if response
        .content_length()
        .is_some_and(|len| len > MAX_DESCRIPTOR_BYTES as u64)
    {
        return None;
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.ok()? {
        if chunk.len() > MAX_DESCRIPTOR_BYTES - bytes.len() {
            return None;
        }
        bytes.extend_from_slice(&chunk);
    }
    Some(bytes)
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct HostedRelease {
    version: String,
    image: String,
    release_notes_url: String,
}

fn release_status(current: &str, bytes: &[u8]) -> Option<Value> {
    let release: HostedRelease = serde_json::from_slice(bytes).ok()?;
    let latest = Version::parse(&release.version).ok()?;
    let installed = Version::parse(current).ok()?;
    let sha = release.image.strip_prefix(IMAGE_PREFIX)?;
    if !latest.pre.is_empty()
        || !latest.build.is_empty()
        || sha.len() != 40
        || !sha
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
        || release.release_notes_url != format!("{RELEASES}/tag/v{latest}")
    {
        return None;
    }
    Some(json!({
        "current": current,
        "latest": release.version,
        "available": latest > installed,
        "releaseNotesUrl": release.release_notes_url
    }))
}

fn unavailable(current: &str) -> Value {
    json!({"current": current, "latest": null, "available": false})
}

#[cfg(test)]
mod tests {
    use super::*;

    fn release(version: &str) -> Value {
        json!({
            "version": version,
            "image": format!("{IMAGE_PREFIX}{}", "a".repeat(40)),
            "releaseNotesUrl": format!("{RELEASES}/tag/v{version}")
        })
    }

    #[test]
    fn compares_published_stable_release_to_installed_version() {
        let bytes = serde_json::to_vec(&release("2.10.0")).unwrap();
        for (installed, available) in [
            ("2.9.1", true),
            ("2.10.0-rc.1", true),
            ("2.10.0", false),
            ("2.10.1", false),
        ] {
            assert_eq!(
                release_status(installed, &bytes).unwrap(),
                json!({
                    "current": installed,
                    "latest": "2.10.0",
                    "available": available,
                    "releaseNotesUrl": format!("{RELEASES}/tag/v2.10.0")
                })
            );
        }
    }

    #[test]
    fn ignores_preview_invalid_and_unofficial_descriptors() {
        for version in ["2.10.0-rc.1", "2.10.0+dev", "latest", "v2.10.0"] {
            let bytes = serde_json::to_vec(&release(version)).unwrap();
            assert!(release_status("2.9.1", &bytes).is_none(), "{version}");
        }
        for (field, value) in [
            ("image", "ghcr.io/yiminspace/quarry:hosted"),
            (
                "image",
                "ghcr.io/other/quarry:sha-0123456789abcdef0123456789abcdef01234567",
            ),
            ("releaseNotesUrl", "https://example.com/releases/2.10.0"),
            (
                "releaseNotesUrl",
                "https://github.com/yiminspace/quarry/releases/tag/v2.9.1",
            ),
        ] {
            let mut descriptor = release("2.10.0");
            descriptor[field] = json!(value);
            let bytes = serde_json::to_vec(&descriptor).unwrap();
            assert!(
                release_status("2.9.1", &bytes).is_none(),
                "{field}: {value}"
            );
        }
        assert!(release_status("2.9.1", b"not a release descriptor").is_none());
    }

    #[test]
    fn restricts_metadata_redirects_to_github_release_urls() {
        for url in [
            "https://github.com/yiminspace/quarry/releases/download/v2.10.0/hosted-release.json",
            "https://release-assets.githubusercontent.com/github-production-release-asset/123",
        ] {
            assert!(is_release_host(&Url::parse(url).unwrap()), "{url}");
        }
        for url in [
            "http://github.com/yiminspace/quarry/releases/asset",
            "https://github.com/other/quarry/releases/asset",
            "https://github.com@outside.example/yiminspace/quarry/releases/asset",
            "https://release-assets.githubusercontent.com:8443/asset",
        ] {
            assert!(!is_release_host(&Url::parse(url).unwrap()), "{url}");
        }
    }

    #[tokio::test]
    async fn disabled_check_returns_current_version_without_fetching() {
        let check = UpdateCheck {
            client: None,
            cache: Mutex::new(None),
        };
        assert_eq!(
            check.status().await,
            json!({"current": CURRENT, "latest": null, "available": false})
        );
    }
}
