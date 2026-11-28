//! Black-box tests of the config: the checks every command runs on it, and whooper settings, which
//! reads and saves the part a settings form edits.

mod common;

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use common::*;
use serde_json::{Value, json};

fn root() -> &'static Path {
    Path::new(env!("CARGO_MANIFEST_DIR"))
}

fn example() -> String {
    fs::read_to_string(root().join("examples/config.example.toml")).unwrap()
}

/// The test config without its [schedule], so Whooper's defaults apply.
fn plain_config() -> String {
    let start = CONFIG.find("[schedule]").unwrap();
    let end = CONFIG.find("[repos.").unwrap();
    format!("{}{}", &CONFIG[..start], &CONFIG[end..])
}

fn use_config(w: &Whooper, text: &str) {
    fs::write(&w.config, text).unwrap();
}

fn config_text(w: &Whooper) -> String {
    fs::read_to_string(&w.config).unwrap()
}

fn names(folder: &Path) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(folder)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

/// The files beside the config, which a save must leave as they were, without a temporary file.
fn config_folder(w: &Whooper) -> Vec<String> {
    names(w.config.parent().unwrap())
}

fn err(out: &std::process::Output) -> String {
    String::from_utf8_lossy(&out.stderr).trim_end().to_string()
}

/// Runs args and returns the error output, failing the test unless it exits 1.
fn refused(w: &Whooper, args: &[&str]) -> String {
    let out = w.run(args);
    assert_eq!(out.status.code(), Some(1), "{args:?}: {}", err(&out));
    err(&out)
}

fn settings(w: &Whooper) -> Value {
    serde_json::from_str(&w.ok(&["settings", "get"])).unwrap()
}

fn see(w: &Whooper) -> String {
    format!("; see {}", w.config.display())
}

#[test]
fn the_example_config_loads_with_every_value_whooper_reads() {
    let w = Whooper::new();
    let config = w.config.clone();
    let example = root().join("examples/config.example.toml");
    let mut status = w.command(&["status"]);
    status.env("WHOOPER_CONFIG", &example);
    assert!(status.output().unwrap().status.success());
    let mut get = w.command(&["settings", "get"]);
    get.env("WHOOPER_CONFIG", &example);
    let got: Value = serde_json::from_slice(&get.output().unwrap().stdout).unwrap();
    assert_eq!(got["jobs"], json!({"review": "devin", "poll": "devin"}));
    assert_eq!(got["ask"]["timeout_minutes"], 10);
    assert_eq!(got["schedule"]["notify"], "notify.sh");
    assert!(config.exists());
}

#[test]
fn settings_get_fills_in_whoopers_defaults() {
    let w = Whooper::new();
    use_config(&w, &plain_config());
    assert_eq!(
        settings(&w),
        json!({
            "schedule": {"interval_minutes": 15, "start": "10:00", "end": "18:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
            "poll": {"timeout_minutes": 10},
            "jobs": {"review": "claude", "poll": "claude"},
            "agents": {"claude": {"review_model": "review-model", "poll_model": "poll-model", "budget_usd": 15},
                       "devin": {"review_model": "devin-review-model", "poll_model": "devin-poll-model"}},
            "review": {"timeout_minutes": 45, "skills": ["review-skill", "language-skill"]},
            "ask": {"timeout_minutes": 10},
        })
    );
}

#[test]
fn a_config_without_each_of_the_two_jobs_fails_naming_the_missing_one() {
    let w = Whooper::new();
    for (missing, text) in [
        ("review", CONFIG.replacen("review = \"claude\"\n", "", 1)),
        ("poll", CONFIG.replacen("poll = \"claude\"\n", "", 1)),
        (
            "review or poll",
            CONFIG.replacen("[jobs]\nreview = \"claude\"\npoll = \"claude\"\n", "", 1),
        ),
    ] {
        use_config(&w, &text);
        assert_eq!(
            refused(&w, &["status"]),
            format!(
                "whooper: [jobs] names no agent for {missing}; each of review and poll names the agent it runs on{}",
                see(&w)
            )
        );
    }
}

#[test]
fn a_config_that_still_names_the_removed_summary_job_loads() {
    // Whooper ran a Slack summary job until it was removed; a config written for it keeps working.
    let w = Whooper::new();
    w.set_config(
        "poll = \"claude\"\n",
        "poll = \"claude\"\nsummary = \"claude\"\n",
    );
    w.ok(&["status"]);
    assert_eq!(
        settings(&w)["jobs"],
        json!({"review": "claude", "poll": "claude"})
    );
}

#[test]
fn each_time_limit_whooper_cannot_keep_stops_every_command() {
    let w = Whooper::new();
    let review = "whooper: [review] timeout_minutes takes a whole number of minutes, at least 1";
    let poll = "whooper: [poll] timeout_minutes takes a whole number of minutes, at least 5";
    let ask = "whooper: [ask] timeout_minutes takes a number of minutes above 0";
    let mut cases: Vec<(String, Option<&str>)> = vec![];
    for value in ["45.0", "1.5", "0", "0.0", "-5", "\"45\"", "true"] {
        cases.push((
            CONFIG.replacen(
                "timeout_minutes = 45",
                &format!("timeout_minutes = {value}"),
                1,
            ),
            Some(review),
        ));
    }
    cases.push((
        CONFIG.replacen("timeout_minutes = 45\n", "", 1),
        Some(review),
    ));
    cases.push((
        CONFIG.replacen("timeout_minutes = 45", "timeout_minutes = 1", 1),
        None,
    ));
    for value in ["7.0", "3", "4.0", "7.5", "\"7\"", "true"] {
        cases.push((
            format!("{CONFIG}\n[poll]\ntimeout_minutes = {value}\n"),
            Some(poll),
        ));
    }
    cases.push((format!("{CONFIG}\n[poll]\ntimeout_minutes = 7\n"), None));
    for value in ["0", "\"7\"", "true"] {
        cases.push((
            format!("{CONFIG}\n[ask]\ntimeout_minutes = {value}\n"),
            Some(ask),
        ));
    }
    for value in ["7", "2.5"] {
        cases.push((
            format!("{CONFIG}\n[ask]\ntimeout_minutes = {value}\n"),
            None,
        ));
    }
    for (text, why) in cases {
        use_config(&w, &text);
        match why {
            Some(why) => assert_eq!(
                refused(&w, &["status"]),
                format!("{why}{}", see(&w)),
                "{text}"
            ),
            None => {
                w.ok(&["status"]);
            }
        }
    }
}

#[test]
fn the_limits_a_config_sets_are_the_ones_settings_reads() {
    let w = Whooper::new();
    assert_eq!(settings(&w)["poll"]["timeout_minutes"], 10);
    assert_eq!(settings(&w)["ask"]["timeout_minutes"], 10);
    use_config(
        &w,
        &format!("{CONFIG}\n[poll]\ntimeout_minutes = 7\n[ask]\ntimeout_minutes = 2.5\n"),
    );
    assert_eq!(settings(&w)["poll"]["timeout_minutes"], 7);
    assert_eq!(settings(&w)["ask"]["timeout_minutes"], 2.5);
}

#[test]
fn set_refuses_a_save_that_leaves_a_job_without_an_agent() {
    let w = Whooper::new();
    let broken = example().replacen("poll = \"devin\"\n", "", 1);
    use_config(&w, &broken);
    assert_eq!(
        refused(
            &w,
            &[
                "settings",
                "set",
                r#"{"schedule": {"interval_minutes": 30}}"#
            ]
        ),
        format!(
            "whooper: [jobs] names no agent for poll; each of review and poll names the agent it runs on{}",
            see(&w)
        )
    );
    assert_eq!(config_text(&w), broken);
    // A save that gives the job an agent goes through.
    w.ok(&[
        "settings",
        "set",
        r#"{"schedule": {"interval_minutes": 30}, "jobs": {"poll": "claude"}}"#,
    ]);
    assert_eq!(
        settings(&w)["jobs"],
        json!({"review": "devin", "poll": "claude"})
    );
}

#[test]
fn set_refuses_a_save_that_leaves_a_time_limit_whooper_refuses_to_start_on() {
    let w = Whooper::new();
    let review = "[review] timeout_minutes takes a whole number of minutes, at least 1";
    let ask_line = "timeout_minutes = 10               # whooper-ask";
    for (old, new, why) in [
        ("timeout_minutes = 45 ", "timeout_minutes = 1.5 ", review),
        ("timeout_minutes = 45 ", "timeout_minutes = 0 ", review),
        ("timeout_minutes = 45 ", "timeout_minutes = \"45\" ", review),
        ("timeout_minutes = 45 ", "timeout_minutes = true ", review),
        (
            "timeout_minutes = 10 ",
            "timeout_minutes = 3 ",
            "[poll] timeout_minutes takes a whole number of minutes, at least 5",
        ),
        (
            ask_line,
            "timeout_minutes = 0               # whooper-ask",
            "[ask] timeout_minutes takes a number of minutes above 0",
        ),
    ] {
        let edited = example().replacen(old, new, 1);
        assert_ne!(edited, example());
        use_config(&w, &edited);
        let save = refused(
            &w,
            &[
                "settings",
                "set",
                r#"{"schedule": {"interval_minutes": 30}}"#,
            ],
        );
        assert_eq!(save, format!("whooper: {why}{}", see(&w)));
        assert_eq!(refused(&w, &["status"]), save);
        assert_eq!(config_text(&w), edited);
        assert_eq!(config_folder(&w), ["config.toml", "notes.md", "notify.sh"]);
    }
}

#[test]
fn get_reads_a_review_limit_whooper_refuses_and_a_save_that_fixes_it_goes_through() {
    let w = Whooper::new();
    for (bad, read) in [
        ("1.5", json!(1.5)),
        ("0", json!(0)),
        ("\"45\"", json!("45")),
    ] {
        use_config(
            &w,
            &example().replacen(
                "timeout_minutes = 45 ",
                &format!("timeout_minutes = {bad} "),
                1,
            ),
        );
        assert_eq!(settings(&w)["review"]["timeout_minutes"], read);
        w.ok(&["settings", "set", r#"{"review": {"timeout_minutes": 30}}"#]);
        assert_eq!(
            config_text(&w),
            example().replacen("timeout_minutes = 45 ", "timeout_minutes = 30 ", 1)
        );
    }
}

#[test]
fn set_rewrites_a_line_whose_value_equals_the_saved_one_but_has_another_type() {
    let w = Whooper::new();
    for (line, edited, values, saved) in [
        (
            "interval_minutes = 15\n",
            "interval_minutes = true\n",
            r#"{"schedule": {"interval_minutes": 1}}"#,
            "interval_minutes = 1\n",
        ),
        (
            "timeout_minutes = 45 ",
            "timeout_minutes = true ",
            r#"{"review": {"timeout_minutes": 1}}"#,
            "timeout_minutes = 1 ",
        ),
    ] {
        use_config(&w, &example().replacen(line, edited, 1));
        w.ok(&["settings", "set", values]);
        assert_eq!(config_text(&w), example().replacen(line, saved, 1));
    }
}

#[test]
fn a_whole_number_sent_as_a_float_is_refused() {
    let w = Whooper::new();
    for (values, place) in [
        (
            r#"{"review": {"timeout_minutes": 45.0}}"#,
            "settings.review.timeout_minutes",
        ),
        (
            r#"{"schedule": {"interval_minutes": 20.0}}"#,
            "settings.schedule.interval_minutes",
        ),
    ] {
        use_config(&w, &example());
        assert!(refused(&w, &["settings", "set", values]).contains(place));
        assert_eq!(config_text(&w), example());
    }
}

#[test]
fn a_save_takes_the_ask_limits_whooper_ask_starts_on_and_refuses_the_rest() {
    let w = Whooper::new();
    let line = "timeout_minutes = 10               # whooper-ask";
    for (text, works) in [
        ("2.5", true),
        ("0.05", true),
        ("15", true),
        ("0", false),
        ("-1", false),
        ("true", false),
        ("\"7\"", false),
    ] {
        use_config(&w, &example());
        let values = format!(r#"{{"ask": {{"timeout_minutes": {text}}}}}"#);
        let edited = example().replacen(line, &line.replace("10 ", &format!("{text} ")), 1);
        if works {
            w.ok(&["settings", "set", &values]);
            assert_eq!(config_text(&w), edited);
            w.ok(&["status"]);
        } else {
            refused(&w, &["settings", "set", &values]);
            assert_eq!(config_text(&w), example());
            use_config(&w, &edited);
            refused(&w, &["status"]);
        }
    }
}

#[test]
fn set_changes_only_the_given_line_and_keeps_every_comment() {
    let w = Whooper::new();
    for (values, old, new) in [
        (
            r#"{"schedule": {"interval_minutes": 30}}"#,
            "interval_minutes = 15\n",
            "interval_minutes = 30\n",
        ),
        // The changed line keeps its own comment too.
        (
            r#"{"poll": {"timeout_minutes": 5}}"#,
            "timeout_minutes = 10 ",
            "timeout_minutes = 5 ",
        ),
        (
            r#"{"ask": {"timeout_minutes": 15}}"#,
            "timeout_minutes = 10               # whooper-ask",
            "timeout_minutes = 15               # whooper-ask",
        ),
    ] {
        use_config(&w, &example());
        w.ok(&["settings", "set", values]);
        // [poll] and [ask] each have the line timeout_minutes = 10, and a change to one keeps the other.
        assert_eq!(config_text(&w), example().replacen(old, new, 1));
    }
}

#[test]
fn a_claude_budget_is_optional_and_can_be_set() {
    // With no budget_usd, as in the example config, a Claude review has no spending limit.
    let w = Whooper::new();
    use_config(&w, &example());
    assert!(settings(&w)["agents"]["claude"].get("budget_usd").is_none());
    w.ok(&[
        "settings",
        "set",
        r#"{"agents": {"claude": {"budget_usd": 7.5}}}"#,
    ]);
    assert_eq!(settings(&w)["agents"]["claude"]["budget_usd"], 7.5);
}

#[test]
fn two_saves_at_once_both_change_the_file() {
    // Prompting Time's settings form saves the schedule while a terminal saves the ask limit.
    let w = Whooper::new();
    for _ in 0..5 {
        use_config(&w, &example());
        let mut form = w
            .command(&[
                "settings",
                "set",
                r#"{"schedule": {"interval_minutes": 30}}"#,
            ])
            .spawn()
            .unwrap();
        let mut terminal = w
            .command(&["settings", "set", r#"{"ask": {"timeout_minutes": 15}}"#])
            .spawn()
            .unwrap();
        assert!(form.wait().unwrap().success());
        assert!(terminal.wait().unwrap().success());
        let saved = example()
            .replacen("interval_minutes = 15\n", "interval_minutes = 30\n", 1)
            .replacen(
                "timeout_minutes = 10               # whooper-ask",
                "timeout_minutes = 15               # whooper-ask",
                1,
            );
        assert_eq!(config_text(&w), saved);
    }
}

#[test]
fn set_adds_a_missing_table() {
    let w = Whooper::new();
    use_config(&w, &plain_config());
    w.ok(&[
        "settings",
        "set",
        r#"{"schedule": {"interval_minutes": 30}}"#,
    ]);
    assert_eq!(
        config_text(&w),
        format!("{}\n[schedule]\ninterval_minutes = 30\n", plain_config())
    );
    assert_eq!(
        settings(&w)["schedule"],
        json!({"interval_minutes": 30, "start": "10:00", "end": "18:00", "days": ["mon", "tue", "wed", "thu", "fri"]})
    );
}

#[test]
fn saving_every_value_get_printed_changes_nothing() {
    // A form may send back every field, and rewriting an unchanged value could reformat its line.
    let w = Whooper::new();
    let text = example().replacen(
        "days = [\"mon\", \"tue\", \"wed\", \"thu\", \"fri\"]",
        "days = [\n  \"mon\",  # standup\n  \"tue\", \"wed\", \"thu\", \"fri\",\n]",
        1,
    );
    use_config(&w, &text);
    let values = w.ok(&["settings", "get"]);
    w.ok(&["settings", "set", &values]);
    assert_eq!(config_text(&w), text);
}

#[test]
fn bad_values_are_rejected_and_leave_the_file_unchanged() {
    let w = Whooper::new();
    for (text, why) in [
        (
            r#"{"schedule": {"interval_minutes": 0}}"#,
            "interval_minutes is below 1",
        ),
        (
            r#"{"schedule": {"interval_minutes": "30"}}"#,
            "interval_minutes is not of type integer",
        ),
        // A poll takes 2 to 3 minutes, so a shorter limit would stop every poll.
        (
            r#"{"poll": {"timeout_minutes": 4}}"#,
            "timeout_minutes is below 5",
        ),
        (
            r#"{"ask": {"timeout_minutes": 0}}"#,
            "settings.ask.timeout_minutes is not above 0",
        ),
        (
            r#"{"review": {"timeout_minutes": 1.5}}"#,
            "settings.review.timeout_minutes is not of type integer",
        ),
        (
            r#"{"agents": {"claude": {"budget_usd": 0}}}"#,
            "budget_usd is below 1",
        ),
        (r#"{"schedule": {"interval": 30}}"#, "unexpected interval"),
        (
            r#"{"slack": {"ask_channel": "C0000000009"}}"#,
            "unexpected slack",
        ),
        (r#"{"schedule": {"days": ["mon", "tues"]}}"#, "\"tues\""),
        (r#"{"jobs": {"review": "nobody"}}"#, "\"nobody\""),
        (
            r#"{"schedule": {"start": "18:00", "end": "10:00"}}"#,
            "an end after its start",
        ),
        // The config's start is 10:00, and the check reads the config as it would be saved.
        (
            r#"{"schedule": {"end": "09:00"}}"#,
            "an end after its start",
        ),
        (r#"{"schedule": {"start": "9:30am"}}"#, "24-hour time"),
        (r#"{"schedule": {interval_minutes: 30}}"#, "not JSON"),
    ] {
        use_config(&w, &example());
        let why_printed = refused(&w, &["settings", "set", text]);
        assert!(why_printed.contains(why), "{text}: {why_printed}");
        assert_eq!(config_text(&w), example());
        assert_eq!(config_folder(&w), ["config.toml", "notes.md", "notify.sh"]);
    }
}

#[test]
fn set_runs_the_jobs_check_on_the_config_as_it_would_be_saved() {
    let w = Whooper::new();
    let broken = example().replacen(
        "[jobs]\nreview = \"devin\"",
        "[jobs]\nreview = \"nobody\"",
        1,
    );
    use_config(&w, &broken);
    assert!(
        refused(
            &w,
            &[
                "settings",
                "set",
                r#"{"schedule": {"interval_minutes": 30}}"#
            ]
        )
        .contains("agents/nobody/review")
    );
    assert_eq!(config_text(&w), broken);
    w.ok(&["settings", "set", r#"{"jobs": {"review": "claude"}}"#]);
    assert_eq!(
        config_text(&w),
        example().replacen(
            "[jobs]\nreview = \"devin\"",
            "[jobs]\nreview = \"claude\"",
            1
        )
    );
}

#[test]
fn set_writes_through_a_link_and_keeps_the_files_mode() {
    // A config kept in a dotfiles folder is often a link to it.
    let w = Whooper::new();
    use_config(&w, &example());
    let target: PathBuf = w.config.with_file_name("dotfiles-config.toml");
    fs::rename(&w.config, &target).unwrap();
    std::os::unix::fs::symlink(&target, &w.config).unwrap();
    fs::set_permissions(&target, fs::Permissions::from_mode(0o640)).unwrap();
    w.ok(&[
        "settings",
        "set",
        r#"{"schedule": {"interval_minutes": 30}}"#,
    ]);
    assert!(
        fs::symlink_metadata(&w.config)
            .unwrap()
            .file_type()
            .is_symlink()
    );
    assert_eq!(
        fs::read_to_string(&target).unwrap(),
        example().replacen("interval_minutes = 15\n", "interval_minutes = 30\n", 1)
    );
    assert_eq!(
        fs::metadata(&target).unwrap().permissions().mode() & 0o777,
        0o640
    );
}

#[test]
fn a_failed_write_leaves_the_old_file_and_no_temporary_file() {
    let w = Whooper::new();
    use_config(&w, &example());
    let folder = w.config.parent().unwrap().to_path_buf();
    fs::set_permissions(&folder, fs::Permissions::from_mode(0o555)).unwrap();
    let out = w.run(&[
        "settings",
        "set",
        r#"{"schedule": {"interval_minutes": 30}}"#,
    ]);
    fs::set_permissions(&folder, fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(config_text(&w), example());
    assert_eq!(config_folder(&w), ["config.toml", "notes.md", "notify.sh"]);
}

#[test]
fn settings_refuses_other_arguments_with_its_usage() {
    let w = Whooper::new();
    assert_eq!(
        refused(&w, &["settings", "put"]),
        "usage: whooper settings get | set <json>"
    );
}
