//! The gh calls Whooper makes, with the user's own GitHub login.

use std::io::Write;
use std::process::{Command, Stdio};

use serde_json::Value;

use crate::fail;
use crate::fsx::Result;

/// The head commit of repo's pull request pr now.
pub fn head(repo: &str, pr: &str) -> Result<String> {
    let out = Command::new("gh")
        .args([
            "api",
            &format!("repos/{repo}/pulls/{pr}"),
            "--jq",
            ".head.sha",
        ])
        .stderr(Stdio::inherit())
        .output()?;
    if !out.status.success() {
        return Err(crate::fsx::Exit::new(
            out.status.code().unwrap_or(1),
            String::new(),
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Posts request as a review of repo's pull request pr, and returns GitHub's response, or gh's error
/// output and output when it fails.
pub fn post_review(repo: &str, pr: &str, request: &Value) -> std::result::Result<Value, String> {
    let mut file = tempfile::NamedTempFile::new().map_err(|e| e.to_string())?;
    file.write_all(serde_json::to_string_pretty(request).unwrap().as_bytes())
        .map_err(|e| e.to_string())?;
    let path = file.path().to_string_lossy().into_owned();
    let out = Command::new("gh")
        .args([
            "api",
            "--method",
            "POST",
            &format!("repos/{repo}/pulls/{pr}/reviews"),
            "--input",
            &path,
        ])
        .output()
        .map_err(|e| e.to_string())?;
    let (stdout, stderr) = (
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    );
    if !out.status.success() {
        return Err(format!("{stderr}{stdout}"));
    }
    serde_json::from_str(&stdout).map_err(|_| format!("{stderr}{stdout}"))
}

/// The pull request at url as queries/pr.graphql reads it, or None when GitHub has no such PR.
pub fn pull_request(url: &str) -> Result<Option<Value>> {
    let query = format!(
        "query=@{}",
        crate::config::root()
            .join("queries")
            .join("pr.graphql")
            .display()
    );
    let out = Command::new("gh")
        .args([
            "api",
            "graphql",
            "-F",
            &query,
            "-f",
            &format!("url={url}"),
            "--jq",
            ".data.resource | select(.url != null)",
        ])
        .stderr(Stdio::inherit())
        .output()?;
    if !out.status.success() {
        fail!("");
    }
    let text = String::from_utf8_lossy(&out.stdout);
    Ok(serde_json::from_str(text.trim()).ok())
}
