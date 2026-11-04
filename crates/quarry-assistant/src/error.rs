use std::fmt;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AssistantError {
    pub status: u16,
    pub code: &'static str,
    pub message: String,
}
impl AssistantError {
    pub fn invalid(message: impl Into<String>) -> Self {
        Self {
            status: 400,
            code: "INVALID_REQUEST",
            message: message.into(),
        }
    }
    pub fn conflict(message: impl Into<String>) -> Self {
        Self {
            status: 409,
            code: "CONFLICT",
            message: message.into(),
        }
    }
    pub fn not_found(message: impl Into<String>) -> Self {
        Self {
            status: 404,
            code: "NOT_FOUND",
            message: message.into(),
        }
    }
    pub fn unavailable(message: impl Into<String>) -> Self {
        Self {
            status: 503,
            code: "UNAVAILABLE",
            message: message.into(),
        }
    }
    pub fn cancelled() -> Self {
        Self {
            status: 499,
            code: "CANCELLED",
            message: "Generation stopped.".into(),
        }
    }
}
impl fmt::Display for AssistantError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for AssistantError {}
pub type Result<T> = std::result::Result<T, AssistantError>;
pub(crate) fn text<'a>(
    value: &'a serde_json::Value,
    name: &str,
    limit: usize,
    required: bool,
) -> Result<&'a str> {
    let v = value.as_str().ok_or_else(|| {
        AssistantError::invalid(format!(
            "{name} must be text with at most {limit} characters."
        ))
    })?;
    if v.chars().count() > limit || v.contains('\0') || (required && v.trim().is_empty()) {
        return Err(AssistantError::invalid(format!(
            "{name} must be text with at most {limit} characters."
        )));
    }
    Ok(v)
}
