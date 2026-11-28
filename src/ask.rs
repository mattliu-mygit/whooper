//! whooper-ask: answers a question about a review with an agent that may read the review's record.

use std::path::{Path, PathBuf};

use crate::fsx;

/// whooper-ask's saved answers about the review in run, asks/1.json, asks/2.json, and so on, in
/// number order.
pub fn ask_files(run: &Path) -> Vec<PathBuf> {
    let mut found: Vec<(u64, PathBuf)> = fsx::entries(&run.join("asks"))
        .into_iter()
        .filter_map(|p| {
            let name = fsx::name_of(&p);
            let stem = name.strip_suffix(".json")?;
            if stem.is_empty() || !stem.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            Some((stem.parse().ok()?, p.clone()))
        })
        .collect();
    found.sort();
    found.into_iter().map(|(_, p)| p).collect()
}

use std::fs;
use std::io::Write;
use std::time::Duration;

use serde_json::{Value, json};

use crate::config::{AGENTS, Whooper};
use crate::fsx::{Exit, Result};
use crate::proc::{self, Ended};

fn die(message: impl Into<String>) -> Exit {
    Exit::new(1, format!("whooper-ask: {}", message.into()))
}

/// whooper ask <run-id> <question> [--finding <finding-id>]: answers a question about a review run with
/// a new run of the agent that did the review, prints the answer, and saves it as asks/<n>.json in the
/// run's directory. A review a live whooper review still runs gets no ask. A review stopped before it
/// wrote run.json left failure.txt and recorded no agent, so the config's review agent answers about it.
/// The new run may read the review's record and write nothing but its answer, has no GitHub login, and
/// has every Slack write denied. A run that passes [ask] timeout_minutes is stopped with everything it
/// started, and saves no answer; its number is not used again.
pub fn ask(args: &[String]) -> Result<()> {
    let usage = || die("usage: whooper ask <run-id> <question> [--finding <finding-id>]");
    let (mut run_id, mut question, mut finding) = (None, None, None);
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        if arg == "--finding" {
            finding = Some(rest.next().ok_or_else(usage)?.clone());
        } else if run_id.is_none() {
            run_id = Some(arg.clone());
        } else if question.is_none() {
            question = Some(arg.clone());
        } else {
            return Err(usage());
        }
    }
    let whooper = Whooper::load()?;
    let (Some(run_id), Some(question)) = (
        run_id.filter(|r| !r.is_empty()),
        question.filter(|q| !q.is_empty()),
    ) else {
        return Err(usage());
    };
    let state = whooper.state.clone();
    let run = crate::status::run_dir(&state, &run_id, "whooper-ask")?;
    let agent = match fsx::read_object(&run.join("run.json")) {
        Some(ran) => {
            let lock = state.join("locks").join(format!(
                "{}-{}",
                fsx::str_of(&ran, "repo").unwrap_or("").replace('/', "-"),
                ran.get("pr").cloned().unwrap_or(Value::Null)
            ));
            if let Some(pid) = crate::lock::pid_in(&lock)
                && fsx::read_text(&lock.join("run_id")).is_some_and(|r| r.trim() == run_id)
                && crate::lock::runs(pid, "whooper-review")
            {
                return Err(die(format!(
                    "the review is still running, in process {pid}; ask again once it ends"
                )));
            }
            fsx::str_of(&ran, "agent").unwrap_or("null").to_string()
        }
        None if run.join("failure.txt").is_file() => whooper.agent("review"),
        None => {
            return Err(die(format!(
                "no run {run_id} in {}",
                state.join("runs").display()
            )));
        }
    };
    if !AGENTS.contains(&agent.as_str()) {
        return Err(die(format!(
            "{} ran on {agent}, which Whooper cannot run",
            run.display()
        )));
    }
    proc::catch_signals();
    let work = tempfile::tempdir()?;
    let text = crate::prompt::ask(&whooper, &agent, &run, &question, finding.as_deref())?;
    let asks = run.join("asks");
    fs::create_dir_all(&asks)?;
    // Of two asks started at once each takes its own number, and a number an earlier ask took is not
    // used again.
    let mut n = 1;
    loop {
        let path = asks.join(format!("{n}.prompt.md"));
        match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(mut file) => {
                file.write_all(text.as_bytes())?;
                break;
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => n += 1,
            Err(_) => return Err(die(format!("cannot write {}", path.display()))),
        }
    }
    let started = jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%SZ")
        .to_string();
    let answer = work.path().join("answer.md");
    let prefix = asks.join(n.to_string());
    let minutes = whooper.limits.ask;
    let env = vec![
        ("GH_TOKEN".to_string(), "disabled".to_string()),
        ("GIT_SSH_COMMAND".into(), "false".into()),
        ("GIT_TERMINAL_PROMPT".into(), "0".into()),
        ("WHOOPER_RUN".into(), run.display().to_string()),
    ];
    let ended = crate::agent::run(
        &whooper,
        &agent,
        &crate::agent::Job::Ask {
            prompt: &asks.join(format!("{n}.prompt.md")),
            answer: &answer,
            prefix: &prefix,
            run: &run,
        },
        Duration::from_secs_f64(minutes * 60.0),
        None,
        &env,
    )?;
    let code = match ended {
        Ended::Stopped => return Err(proc::stopped()),
        Ended::TimedOut => {
            return Err(die(format!(
                "the ask run passed its {}-minute limit; see {}.*",
                fsx::number(minutes),
                prefix.display()
            )));
        }
        Ended::Exited(code) => code,
    };
    let text = fsx::read_text(&answer).unwrap_or_default();
    if code != 0 || text.trim().is_empty() {
        return Err(die(format!(
            "the ask run exited {code} without an answer; see {}.*",
            prefix.display()
        )));
    }
    // Claude's transcript is <n>.transcript.jsonl and Devin's <n>.transcript.json.
    let transcript = fsx::entries(&asks)
        .into_iter()
        .rfind(|p| fsx::name_of(p).starts_with(&format!("{n}.transcript.")) && p.is_file());
    let saved = json!({
        "question": question,
        "finding": finding,
        "answer": text.trim(),
        "agent": agent,
        "started_at": started,
        "ended_at": jiff::Timestamp::now().strftime("%Y-%m-%dT%H:%M:%SZ").to_string(),
        "transcript": transcript.map(|t| t.display().to_string()),
    });
    fsx::write_file(&asks.join(format!("{n}.json")), &fsx::pretty(&saved), false)?;
    println!("{}", text.trim());
    Ok(())
}
