//! The agents Whooper's jobs run on, Claude Code and Devin, and how each runs a review, a poll, and an
//! ask: the CLI's arguments, what it may do, and how its answer and transcript are kept.
//!
//! - A review starts in the PR's head worktree with no GitHub login and loads what a normal session
//!   there loads, with every Slack write denied by name, so the run can post nothing. Claude returns
//!   the findings as its structured output, checked against schemas/findings.json; Devin writes
//!   findings.json itself.
//! - A poll's prompt holds the ask channel's posts, so the run gets no tools: Slack text it reads cannot
//!   make it do anything.
//! - An ask may only read files, the review's record among them, and writes nothing but its answer.
//!
//! Each CLI runs in a session of its own (proc::spawn), so a time limit or a stop ends it with
//! everything it started. Devin Desktop sets ACP_BACKEND and WINDSURF_EXT_HOST_PID in its shells; the
//! CLI ignores its own login while ACP_BACKEND is set, and quits once the process
//! WINDSURF_EXT_HOST_PID names exits, so Devin runs without both. Each Devin session is deleted once
//! its transcript is saved, so runs don't fill Devin Desktop's session list.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::config::{self, Whooper};
use crate::fsx::{self, Result};
use crate::proc::{self, Ended};

/// The Slack tools of Claude's Slack connector that write, which every run is denied.
const CLAUDE_SLACK_WRITES: &str = "mcp__claude_ai_Slack__slack_add_reaction mcp__claude_ai_Slack__slack_create_canvas mcp__claude_ai_Slack__slack_update_canvas mcp__claude_ai_Slack__slack_schedule_message mcp__claude_ai_Slack__slack_send_message_draft";
/// Every tool of the two Slack servers Devin can load.
const DEVIN_SLACK: [&str; 2] = ["mcp__slack-remote__*", "mcp__slack__*"];

/// What a job runs, with its files.
pub enum Job<'a> {
    /// A review: the prompt file, the run directory, and the head worktree it starts in.
    Review { prompt: &'a Path, run: &'a Path },
    /// A poll's judgment: the prompt file, the answer file, and the prefix of its record files.
    Poll {
        prompt: &'a Path,
        answer: &'a Path,
        prefix: &'a Path,
    },
    /// An ask: the prompt file, the answer file, the prefix of its record files, and the review's run
    /// directory.
    Ask {
        prompt: &'a Path,
        answer: &'a Path,
        prefix: &'a Path,
        run: &'a Path,
    },
}

fn with_suffix(prefix: &Path, suffix: &str) -> PathBuf {
    PathBuf::from(format!("{}{suffix}", prefix.display()))
}

fn file(path: &Path) -> Result<fs::File> {
    Ok(fs::File::create(path)?)
}

fn home() -> PathBuf {
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
}

/// The JSON lines of a stream-json transcript.
fn stream(path: &Path) -> Vec<Value> {
    crate::diff::json_lines(&fsx::read_text(path).unwrap_or_default())
}

/// The results of a Claude transcript that succeeded.
fn successes(lines: &[Value]) -> impl Iterator<Item = &Value> {
    lines
        .iter()
        .filter(|l| l["type"] == "result" && l["subtype"] == "success")
}

/// A number as jq prints one: whole numbers without a fraction.
fn jq_number(value: &Value) -> String {
    match value.as_f64() {
        Some(n) => fsx::number(n),
        None => "null".into(),
    }
}

/// The user's Devin config, ~/.config/devin/config.json, as an object, with changes.
fn devin_config(allow_none: bool, deny: &[&str]) -> Value {
    let user = fsx::read_json(&home().join(".config/devin/config.json"));
    let mut config = match user {
        Some(Value::Object(map)) => Value::Object(map),
        _ => json!({}),
    };
    let permissions = config
        .as_object_mut()
        .unwrap()
        .entry("permissions")
        .or_insert_with(|| json!({}));
    if !permissions.is_object() {
        *permissions = json!({});
    }
    if allow_none {
        permissions["allow"] = json!([]);
    }
    let mut denied = permissions
        .get("deny")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    denied.extend(deny.iter().map(|d| json!(d)));
    permissions["deny"] = Value::Array(denied);
    config
}

/// Deletes the Devin session a transcript names.
fn devin_rm(transcript: &Path) {
    if let Some(id) = fsx::read_object(transcript)
        .as_ref()
        .and_then(|t| fsx::str_of(t, "session_id").map(str::to_string))
    {
        let _ = devin_command()
            .args(["rm", &id, "--force"])
            .stdout(Stdio::null())
            .status();
    }
}

fn devin_command() -> Command {
    let mut command = Command::new("devin");
    command
        .env_remove("ACP_BACKEND")
        .env_remove("WINDSURF_EXT_HOST_PID");
    command
}

/// Runs job on agent within limit, in folder, with env added to the environment, and returns how the
/// run ended: its exit code, which fails when the run left no answer, or that it passed its limit or a
/// signal stopped this command, which leave the run's files as they are.
pub fn run(
    whooper: &Whooper,
    agent: &str,
    job: &Job,
    limit: Duration,
    folder: Option<&Path>,
    env: &[(String, String)],
) -> Result<Ended> {
    let work = tempfile::tempdir()?;
    let schemas = config::root().join("schemas");
    let started = Instant::now();
    let mut command;
    match (agent, job) {
        ("claude", Job::Review { prompt, run }) => {
            command = Command::new("claude");
            command
                .env("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
                .arg("-p")
                .arg(fs::read_to_string(prompt)?)
                .args([
                    "--model",
                    &whooper.agent_required("claude", "review_model")?,
                ])
                .args(["--permission-mode", "auto", "--permission-prompts", "none"]);
            // With no [agents.claude] budget_usd, the run has no spending limit.
            if let Some(budget) = whooper.agent_setting("claude", "budget_usd") {
                command.args(["--max-budget-usd", &budget]);
            }
            command
                .arg("--add-dir")
                .arg(run)
                .args(["--disallowedTools", CLAUDE_SLACK_WRITES])
                .arg("--json-schema")
                .arg(fs::read_to_string(schemas.join("findings.json"))?)
                .args([
                    "--no-session-persistence",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                ])
                .stdout(file(&run.join("transcript.jsonl"))?)
                .stderr(file(&run.join("stderr.log"))?);
        }
        ("claude", Job::Poll { prompt, prefix, .. }) => {
            command = Command::new("claude");
            command
                .env("CLAUDE_CODE_DISABLE_CLAUDE_MDS", "1")
                .env("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
                .arg("-p")
                .arg(fs::read_to_string(prompt)?)
                .args(["--model", &whooper.agent_required("claude", "poll_model")?])
                .args([
                    "--setting-sources",
                    "user",
                    "--strict-mcp-config",
                    "--tools",
                    "",
                    "--disable-slash-commands",
                    "--no-session-persistence",
                    "--json-schema",
                ])
                .arg(fs::read_to_string(schemas.join("slack-asks.json"))?)
                .args(["--output-format", "stream-json", "--verbose"])
                .stdout(file(&with_suffix(prefix, ".transcript.jsonl"))?);
        }
        (
            "claude",
            Job::Ask {
                prompt,
                prefix,
                run,
                ..
            },
        ) => {
            // The run starts in an empty folder, so no project's settings or CLAUDE.md files load, and its
            // only tools read and search files or load skills; dontAsk denies any call that would wait for
            // an approval.
            command = Command::new("claude");
            command
                .current_dir(work.path())
                .env("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
                .arg("-p")
                .arg(fs::read_to_string(prompt)?)
                .args([
                    "--model",
                    &whooper.agent_required("claude", "review_model")?,
                ])
                .args([
                    "--tools",
                    "Read,Grep,Glob,Skill",
                    "--permission-mode",
                    "dontAsk",
                    "--add-dir",
                ])
                .arg(run)
                .arg(home().join(".claude/skills"))
                .args(["--disallowedTools", CLAUDE_SLACK_WRITES])
                .args([
                    "--no-session-persistence",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                ])
                .stdout(file(&with_suffix(prefix, ".transcript.jsonl"))?)
                .stderr(file(&with_suffix(prefix, ".stderr.log"))?);
        }
        ("devin", Job::Review { prompt, run }) => {
            // Deny rules hold in dangerous mode, which approves every other call. The config goes in the
            // run directory because the CLI rewrites the file it is given.
            let config = run.join("devin-config.json");
            fsx::write_file(
                &config,
                &fsx::pretty(&devin_config(false, &DEVIN_SLACK)),
                false,
            )?;
            command = devin_command();
            command
                .args(["-p", "--prompt-file"])
                .arg(prompt)
                .arg("--config")
                .arg(&config)
                .args(["--model", &whooper.agent_required("devin", "review_model")?])
                .args([
                    "--permission-mode",
                    "dangerous",
                    "--respect-workspace-trust",
                    "false",
                    "--export",
                ])
                .arg(run.join("transcript.json"))
                .stdout(file(&run.join("stdout.log"))?)
                .stderr(file(&run.join("stderr.log"))?);
        }
        ("devin", Job::Poll { prompt, prefix, .. }) => {
            // accept-edits lets it write files in its empty folder only, and anything else needs an
            // approval nobody gives.
            let text = format!(
                "{}\nWrite your answer to answer.json in the working directory with your file-writing tool, and nothing else. This run rejects shell commands, so write out the whole JSON yourself instead of generating it with a script. It must be JSON that fits this JSON Schema:\n\n{}",
                fs::read_to_string(prompt)?,
                fs::read_to_string(schemas.join("slack-asks.json"))?
            );
            fs::write(work.path().join("prompt.md"), text)?;
            command = devin_command();
            command
                .current_dir(work.path())
                .args(["-p", "--prompt-file", "prompt.md"])
                .args(["--model", &whooper.agent_required("devin", "poll_model")?])
                .args([
                    "--permission-mode",
                    "accept-edits",
                    "--respect-workspace-trust",
                    "false",
                    "--export",
                ])
                .arg(with_suffix(prefix, ".transcript.json"))
                .stdout(Stdio::null())
                .stderr(file(&with_suffix(prefix, ".stderr.log"))?);
        }
        ("devin", Job::Ask { prompt, prefix, .. }) => {
            // None of the user's allow rules, any of which could approve a command or a write outside
            // its folder, and the shell and every Slack tool denied.
            let config = with_suffix(prefix, ".devin-config.json");
            let deny: Vec<&str> = std::iter::once("exec").chain(DEVIN_SLACK).collect();
            fsx::write_file(&config, &fsx::pretty(&devin_config(true, &deny)), false)?;
            let text = format!(
                "{}\nThis run rejects shell commands: read files with your file-reading and search tools, and write your answer to answer.md in the working directory with your file-writing tool. Write no other file.\n",
                fs::read_to_string(prompt)?
            );
            fs::write(work.path().join("prompt.md"), text)?;
            command = devin_command();
            command
                .current_dir(work.path())
                .args(["-p", "--prompt-file", "prompt.md", "--config"])
                .arg(&config)
                .args(["--model", &whooper.agent_required("devin", "review_model")?])
                .args([
                    "--permission-mode",
                    "accept-edits",
                    "--respect-workspace-trust",
                    "false",
                    "--export",
                ])
                .arg(with_suffix(prefix, ".transcript.json"))
                .stdout(Stdio::null())
                .stderr(file(&with_suffix(prefix, ".stderr.log"))?);
        }
        (other, _) => crate::fail!("whooper: no agent {other}"),
    }
    if let Some(folder) = folder {
        command.current_dir(folder);
    }
    for (key, value) in env {
        command.env(key, value);
    }
    command.stdin(Stdio::null());
    let mut child = match proc::spawn(&mut command) {
        Ok(child) => child,
        Err(e) => {
            eprintln!("whooper: could not start {agent}: {e}");
            return Ok(Ended::Exited(127));
        }
    };
    let ended = proc::wait(&mut child, Some(limit));
    let Ended::Exited(status) = ended else {
        return Ok(ended);
    };
    let answer_or = |ok: bool| {
        Ok(Ended::Exited(if status != 0 {
            status
        } else if ok {
            0
        } else {
            1
        }))
    };
    match (agent, job) {
        ("claude", Job::Review { run, .. }) => {
            let lines = stream(&run.join("transcript.jsonl"));
            let outputs: Vec<String> = successes(&lines)
                .filter(|l| !l["structured_output"].is_null())
                .map(|l| l["structured_output"].to_string() + "\n")
                .collect();
            if outputs.is_empty() {
                let _ = fs::remove_file(run.join("findings.json"));
            } else {
                fs::write(run.join("findings.json"), outputs.concat())?;
            }
            for l in lines.iter().filter(|l| l["type"] == "result") {
                let minutes = l["duration_ms"].as_f64().map(|m| (m / 60000.0).floor());
                eprintln!(
                    "whooper-review: ${} in {} minutes, {} turns",
                    jq_number(&l["total_cost_usd"]),
                    minutes.map(fsx::number).unwrap_or("null".into()),
                    jq_number(&l["num_turns"])
                );
            }
            Ok(Ended::Exited(status))
        }
        ("claude", Job::Poll { answer, prefix, .. }) => {
            let lines = stream(&with_suffix(prefix, ".transcript.jsonl"));
            let outputs: Vec<String> = successes(&lines)
                .filter(|l| !l["structured_output"].is_null())
                .map(|l| l["structured_output"].to_string() + "\n")
                .collect();
            if status == 0 {
                fs::write(answer, outputs.concat())?;
            }
            answer_or(!outputs.is_empty())
        }
        ("claude", Job::Ask { answer, prefix, .. }) => {
            let lines = stream(&with_suffix(prefix, ".transcript.jsonl"));
            let outputs: Vec<String> = successes(&lines)
                .filter_map(|l| l["result"].as_str().map(|r| r.to_string() + "\n"))
                .collect();
            if status == 0 {
                fs::write(answer, outputs.concat())?;
            }
            answer_or(!outputs.is_empty())
        }
        ("devin", Job::Review { run, .. }) => {
            let transcript = run.join("transcript.json");
            if let Some(t) = fsx::read_json(&transcript) {
                let turns: Vec<&Value> = t["steps"]
                    .as_array()
                    .map(|s| s.iter().filter(|s| s["source"] == "agent").collect())
                    .unwrap_or_default();
                let acu: f64 = turns
                    .iter()
                    .map(|s| s["extra"]["committed_acu_cost"].as_f64().unwrap_or(0.0))
                    .sum();
                eprintln!(
                    "whooper-review: {} ACU in {} minutes, {} turns",
                    fsx::number((acu * 1000.0).round() / 1000.0),
                    started.elapsed().as_secs() / 60,
                    turns.len()
                );
            }
            if transcript.exists() {
                devin_rm(&transcript);
            }
            Ok(Ended::Exited(status))
        }
        ("devin", Job::Poll { answer, prefix, .. })
        | ("devin", Job::Ask { answer, prefix, .. }) => {
            devin_rm(&with_suffix(prefix, ".transcript.json"));
            if status != 0 {
                return Ok(Ended::Exited(status));
            }
            let name = if matches!(job, Job::Poll { .. }) {
                "answer.json"
            } else {
                "answer.md"
            };
            answer_or(fs::rename(work.path().join(name), answer).is_ok())
        }
        _ => unreachable!(),
    }
}
