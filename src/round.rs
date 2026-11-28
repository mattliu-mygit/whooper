//! whooper round and whooper run.
//!
//! A round fetches, polls, and selects, then reviews each selected item, and runs [schedule] notify as
//! <notify> <title> <message> when a review finishes or fails, unless whooper skip stopped it. When a
//! review stops at a cause that stops every review, such as a setup script that cannot reach Docker,
//! the round starts none of its remaining reviews: each waits, with that cause, and goes on the recheck
//! list. Only one round runs at a time: it holds <state>/round.lock, whose round.json holds when the round
//! started and the PR it reviews. When the round ends it writes last-round.json and adds the round to
//! rounds.jsonl with a reason. It exits 0 when the poll steps succeeded and 1 when one failed.
//!
//! whooper run runs rounds on the config's [schedule] until stopped: the first at once, each later one
//! interval_minutes after the last ended, moved to the next start outside the configured hours and days.
//! Before each wait it writes the next round's Unix time to <state>/next-round. While <state>/paused
//! exists it skips each round it comes to and adds it to the history as skipped; a pause does not
//! survive a restart. Only one copy runs at a time, holding <state>/run.lock. A stop ends the round it
//! runs, with the round's review, and waits for it to end.
//!
//! Fetch, poll, each review, and each round run as processes of their own, in sessions of their own, so
//! a stop of a round or a review stops all it started.

use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

use serde_json::{Value, json};

use crate::config::{self, Whooper};
use crate::fsx::{self, Object, Result, read_object, str_of};
use crate::proc::{self, Ended};
use crate::{lock, status, work};

/// Runs whooper <args> in a session of its own with no input, and returns its exit code; a stop of
/// this command stops it.
fn child(args: &[&str], stdout: Stdio) -> Result<Ended> {
    let mut command = Command::new(crate::fsx::me()?);
    command.args(args).stdin(Stdio::null()).stdout(stdout);
    let mut child = proc::spawn(&mut command)?;
    Ok(proc::wait(&mut child, None))
}

fn local_clock() -> String {
    fsx::now().strftime("%Y-%m-%d %H:%M:%S").to_string()
}

fn notify(whooper: &Whooper, title: &str, message: &str) {
    if let Some(script) = whooper.notify() {
        let ok = Command::new(&script)
            .args([title, message])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|s| s.success());
        if !ok {
            println!("notify failed: {title}");
        }
    }
}

fn save(path: &Path, value: &Value) -> Result<()> {
    fsx::write_file(path, &(fsx::pretty(value) + "\n"), false)?;
    Ok(())
}

/// whooper round.
pub fn round() -> Result<()> {
    let whooper = Whooper::load()?;
    let state = whooper.state.clone();
    let items = state.join("items");
    fs::create_dir_all(&items)?;
    proc::catch_signals();
    let lock_dir = state.join("round.lock");
    let record_path = lock_dir.join("round.json");
    let started = jiff::Timestamp::now().as_second();
    match lock::take(&state, &lock_dir, "whooper-round", |l| {
        save(
            &l.join("round.json"),
            &json!({"started_at": started, "selected": null, "reviewing": null, "reviews": []}),
        )
        .map_err(|e| std::io::Error::other(e.message))
    })? {
        lock::Taken::Mine => {}
        lock::Taken::HeldBy(pid) => crate::fail!(
            "whooper-round: a round is already running, in process {pid}; see {}/pid",
            lock_dir.display()
        ),
    }
    let mut record: Object = read_object(&record_path).unwrap_or_default();
    let mut result = "interrupted";
    let mut reason: Option<String> = None;
    let outcome = (|| -> Result<()> {
        println!("=== {} poll", local_clock());
        let steps: [(&str, &str); 3] = [
            ("The GitHub fetch", "fetch"),
            ("The Slack poll", "poll"),
            ("Selecting the work", "select"),
        ];
        let mut selected_items = vec![];
        for (step, command) in steps {
            let ok = if command == "select" {
                match crate::select::select(&state, config::table(&whooper.values, "repos")) {
                    Ok(work) => {
                        selected_items = work;
                        let text: String = selected_items
                            .iter()
                            .map(|i| serde_json::to_string(i).unwrap() + "\n")
                            .collect();
                        fsx::write_file(&items.join("selected.jsonl"), &text, false)?;
                        true
                    }
                    Err(e) => {
                        if !e.message.is_empty() {
                            eprintln!("{}", e.message);
                        }
                        false
                    }
                }
            } else {
                match child(&[command], Stdio::inherit())? {
                    Ended::Exited(0) => true,
                    Ended::Exited(_) => false,
                    _ => return Err(proc::stopped()),
                }
            };
            proc::check()?;
            if !ok {
                println!("a poll step failed; trying again next round");
                result = "poll_failed";
                reason = Some(format!("{step} failed"));
                return Err(fsx::Exit::new(1, ""));
            }
        }
        println!("selected: {} item(s)", selected_items.len());
        record.insert("selected".into(), json!(selected_items.len()));
        save(&record_path, &Value::Object(record.clone()))?;
        let mut blocked: Option<Value> = None;
        let mut waited = 0;
        for item in &selected_items {
            let pr = format!("{}#{}", str_of(item, "repo").unwrap_or(""), item["pr"]);
            let kind = str_of(item, "kind").unwrap_or("");
            let file = items.join(format!(
                "{}-{}-{kind}.json",
                str_of(item, "repo").unwrap_or("").replacen('/', "-", 1),
                item["pr"]
            ));
            fsx::write_file(&file, &(serde_json::to_string(item)? + "\n"), false)?;
            let push = |record: &mut Object, pr: &str, outcome: &str| {
                if let Some(Value::Array(reviews)) = record.get_mut("reviews") {
                    reviews.push(json!({"pr": pr, "outcome": outcome}));
                }
            };
            if let Some(cause) = &blocked {
                println!(
                    "--- {pr} {kind} waits for the next round: {}",
                    cause["summary"].as_str().unwrap_or("")
                );
                let cause = json!({"id": cause["id"], "summary": cause["summary"], "scope": cause["scope"]});
                work::wait_review(&state, item, &cause, &whooper.agent("review"))?;
                push(&mut record, &pr, "waiting");
                save(&record_path, &Value::Object(record.clone()))?;
                waited += 1;
                continue;
            }
            println!("--- review {pr} {kind}");
            record.insert("reviewing".into(), json!(pr));
            save(&record_path, &Value::Object(record.clone()))?;
            let out = tempfile::NamedTempFile::new()?;
            let ended = child(
                &["review", &file.display().to_string()],
                Stdio::from(out.reopen()?),
            )?;
            let run = fsx::read_text(out.path())
                .unwrap_or_default()
                .lines()
                .next()
                .unwrap_or("")
                .to_string();
            let run = Path::new(&run);
            if proc::stopping() {
                return Err(proc::stopped());
            }
            let outcome = match ended {
                Ended::Exited(0) => {
                    let findings =
                        fsx::read_json(&run.join("findings.json")).unwrap_or(Value::Null);
                    let reason: String = findings["approval"]["reason"]
                        .as_str()
                        .unwrap_or("")
                        .chars()
                        .take(200)
                        .collect();
                    notify(
                        &whooper,
                        &format!("Whooper reviewed {pr}"),
                        &format!(
                            "{}, {} finding(s). {reason}",
                            findings["approval"]["verdict"]
                                .as_str()
                                .unwrap_or("")
                                .replace('_', " "),
                            findings["findings"].as_array().map_or(0, Vec::len)
                        ),
                    );
                    "reviewed"
                }
                Ended::Exited(_)
                    if !run.as_os_str().is_empty()
                        && run.join(status::SKIPPED_BY_USER).exists() =>
                {
                    println!("review stopped: skipped by the user");
                    "failed"
                }
                Ended::Exited(_) => {
                    println!("review failed");
                    notify(
                        &whooper,
                        &format!("Whooper: {pr} review failed"),
                        "See whooper-round's output.",
                    );
                    if !run.as_os_str().is_empty()
                        && let Some(failure) = fsx::read_json(&run.join("failure.json"))
                        && failure["scope"] == "all"
                    {
                        blocked = Some(failure);
                    }
                    "failed"
                }
                _ => return Err(proc::stopped()),
            };
            // A stop during the notification leaves the review listed as interrupted.
            proc::check()?;
            push(&mut record, &pr, outcome);
            record.insert("reviewing".into(), Value::Null);
            save(&record_path, &Value::Object(record.clone()))?;
            proc::check()?;
        }
        result = "done";
        if waited > 0 {
            reason = Some(format!(
                "{}, so {waited} review(s) wait for the next round",
                blocked
                    .as_ref()
                    .and_then(|b| b["summary"].as_str())
                    .unwrap_or("")
            ));
        }
        Ok(())
    })();
    // However the round ends, round.json becomes last-round.json, with a review still running listed
    // as interrupted, and the history gets the round.
    proc::ignore_signals();
    if lock::pid_in(&lock_dir) == Some(std::process::id() as i64) {
        let ended = jiff::Timestamp::now().as_second();
        let mut reviews = record
            .get("reviews")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if let Some(pr) = record.get("reviewing").filter(|r| !r.is_null()) {
            reviews.push(json!({"pr": pr, "outcome": "interrupted"}));
        }
        let selected = record.get("selected").cloned().unwrap_or(Value::Null);
        let last = json!({"started_at": record.get("started_at").cloned().unwrap_or(json!(started)),
                          "ended_at": ended, "result": result, "selected": selected, "reviews": reviews});
        save(&state.join("last-round.json"), &last)?;
        status::add_round(
            &state,
            &json!({"started_at": last["started_at"], "ended_at": ended, "result": result,
                    "selected": if selected.is_null() { json!(0) } else { selected }, "reason": reason}),
        )?;
        let _ = fs::remove_dir_all(&lock_dir);
    }
    match outcome {
        Err(e) if e.code == 130 || proc::stopping() => Err(proc::stopped()),
        other => other,
    }
}

/// whooper run.
pub fn run() -> Result<()> {
    let whooper = Whooper::load()?;
    let state = whooper.state.clone();
    proc::catch_signals();
    let lock_dir = state.join("run.lock");
    match lock::take(&state, &lock_dir, "whooper-run", |_| Ok(()))? {
        lock::Taken::Mine => {}
        lock::Taken::HeldBy(pid) => crate::fail!(
            "whooper-run: process {pid} is already running Whooper; see {}/pid",
            lock_dir.display()
        ),
    }
    // A pause does not survive a restart: Prompting Time starts whooper run each time it opens.
    let _ = fs::remove_file(state.join("paused"));
    let outcome = (|| -> Result<()> {
        let mut ended: Option<i64> = None;
        loop {
            // The schedule is read each time, so a change applies from the next round.
            let schedule = config::read_schedule(&config::load()?.values)?;
            let zone = jiff::tz::TimeZone::system();
            let earliest = match ended {
                Some(t) => {
                    jiff::Timestamp::from_second(t)?
                        .to_zoned(zone.clone())
                        .datetime()
                        + jiff::Span::new().minutes(schedule.interval_minutes)
                }
                None => fsx::now().datetime().round(jiff::Unit::Second)?,
            };
            let when = config::next_round(&schedule, earliest);
            if when > earliest {
                eprintln!(
                    "whooper-run: outside the [schedule] hours; next round at {}",
                    when.strftime("%a %Y-%m-%d %H:%M")
                );
            }
            let next = when.to_zoned(zone)?.timestamp().as_second();
            fsx::write_file(&state.join("next-round"), &format!("{next}\n"), false)?;
            // Sleep counts only time the computer is awake, so short naps, each followed by a look at
            // the clock, start a round that came due during sleep within moments of waking.
            while jiff::Timestamp::now().as_second() < next {
                proc::check()?;
                std::thread::sleep(Duration::from_millis(200));
            }
            let _ = fs::remove_file(state.join("next-round"));
            if state.join("paused").exists() {
                println!(
                    "=== {} skipped this round: Whooper is paused; whooper resume resumes it",
                    local_clock()
                );
                status::add_round(
                    &state,
                    &json!({"started_at": jiff::Timestamp::now().as_second(), "ended_at": null,
                            "result": "skipped", "selected": 0, "reason": "Whooper is paused"}),
                )?;
            } else if child(&["round"], Stdio::inherit())? == Ended::Stopped {
                return Err(proc::stopped());
            }
            proc::check()?;
            ended = Some(jiff::Timestamp::now().as_second());
        }
    })();
    proc::ignore_signals();
    lock::release(&lock_dir);
    outcome
}
