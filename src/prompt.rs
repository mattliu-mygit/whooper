//! The prompts of Whooper's agent runs: prompts/<name>.md filled in from the config, and the end of an
//! ask's prompt, which names the review's record files.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use regex::Regex;
use serde_json::Value;

use crate::config::{self, Whooper, table};
use crate::fail;
use crate::fsx::{Result, read_json, read_text};

/// Replaces each {{name}} in a prompt template. A placeholder with no value is an error, so a prompt
/// never reaches a run half filled.
pub fn fill(template: &str, values: &BTreeMap<&str, String>) -> Result<String> {
    let re = Regex::new(r"\{\{(\w+)\}\}").unwrap();
    let mut out = String::new();
    let mut last = 0;
    for m in re.captures_iter(template) {
        let whole = m.get(0).unwrap();
        let Some(value) = values.get(&m[1]) else {
            fail!(
                "whooper: no value for {{{{{}}}}} in the prompt template",
                &m[1]
            )
        };
        out.push_str(&template[last..whole.start()]);
        out.push_str(value);
        last = whole.end();
    }
    out.push_str(&template[last..]);
    Ok(out)
}

/// Names a config bot list for a prompt: "the CI review bot (ci-bot), the assistant (helper-bot)".
pub fn bot_list(bots: &Value, key: &str) -> String {
    let names: Vec<String> = bots
        .as_array()
        .map(|a| {
            a.iter()
                .map(|b| {
                    format!(
                        "{} ({})",
                        b["label"].as_str().unwrap_or(""),
                        b[key].as_str().unwrap_or("")
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    if names.is_empty() {
        "none".into()
    } else {
        names.join(", ")
    }
}

/// How prompts/review.md names an agent's tools, limits, and way of returning the findings, by key.
/// prompts/ask.md uses skill_tool too.
pub fn wording(agent: &str) -> BTreeMap<&'static str, String> {
    let findings = config::root().join("schemas").join("findings.json");
    let pairs: [(&str, String); 5] = if agent == "devin" {
        [
            ("shell", "exec".into()),
            ("skill_tool", "the skill tool".into()),
            ("output_limit", "exec shows only the start and end of a long output and saves the rest to a file".into()),
            ("probe_rule", " Pass `timeout: 280000`, exec's longest, on every probe's exec call, so the probe finishes inside that call: a call that exec moves to the background does not count.".into()),
            ("answer", format!("Then write the findings to `findings.json` in the run directory, as JSON that fits the JSON Schema in `{}`. A file that does not fit fails the run.", findings.display())),
        ]
    } else {
        [
            ("shell", "Bash".into()),
            ("skill_tool", "the Skill tool".into()),
            ("output_limit", "the transcript keeps only a 2 KB preview of an output over 30,000 characters".into()),
            ("probe_rule", String::new()),
            ("answer", "Return the findings as your structured output, in the JSON format this run requires.".into()),
        ]
    };
    pairs.into_iter().collect()
}

/// The values every prompt takes from the config.
fn common(whooper: &Whooper) -> Result<BTreeMap<&'static str, String>> {
    let skills: Vec<String> = table(&whooper.values, "review")
        .get("skills")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .map(|s| format!("`{}`", s.as_str().unwrap_or("")))
                .collect()
        })
        .unwrap_or_default();
    Ok(BTreeMap::from([
        ("user_name", whooper.get("user", "name")?),
        ("github_login", whooper.login.clone()),
        (
            "review_bots",
            bot_list(
                table(&whooper.values, "github")
                    .get("review_bots")
                    .unwrap_or(&Value::Null),
                "login",
            ),
        ),
        ("skills", skills.join(", ")),
    ]))
}

fn template(name: &str) -> Result<String> {
    Ok(fs::read_to_string(
        config::root().join("prompts").join(format!("{name}.md")),
    )?)
}

/// The poll run's prompt, before the posts.
pub fn poll(whooper: &Whooper) -> Result<String> {
    fill(&template("poll")?, &common(whooper)?)
}

/// A review run's prompt, before the run's own facts.
pub fn review(whooper: &Whooper, repo: &str, agent: &str) -> Result<String> {
    let mut values = common(whooper)?;
    let notes = table(table(&whooper.values, "repos"), repo)
        .get("notes")
        .and_then(Value::as_str);
    values.insert(
        "repo_notes",
        match notes {
            Some(notes) => fs::read_to_string(whooper.dir.join(notes))?
                .trim()
                .to_string(),
            None => "None.".into(),
        },
    );
    let schemas = config::root().join("schemas");
    values.insert(
        "overview_schema",
        schemas.join("overview.json").display().to_string(),
    );
    values.insert(
        "examples_schema",
        schemas.join("examples-request.json").display().to_string(),
    );
    values.extend(wording(agent));
    fill(&template("review")?, &values)
}

/// An ask run's prompt: prompts/ask.md filled in, then the record files and the question (ask_facts).
pub fn ask(
    whooper: &Whooper,
    agent: &str,
    run: &Path,
    question: &str,
    finding: Option<&str>,
) -> Result<String> {
    let mut values = common(whooper)?;
    values.insert("skill_tool", wording(agent)["skill_tool"].clone());
    Ok(fill(&template("ask")?, &values)? + &ask_facts(run, question, finding)?)
}

/// The files of a review's record an ask may read, each with what it holds, in the order the prompt
/// lists them.
const ASK_RECORD: [(&str, &str); 17] = [
    (
        "failure.txt",
        "the reason whooper-review gave for stopping the review before it finished",
    ),
    (
        "log.md",
        "the review's log: what the change does, the evidence for each finding, and the candidates it dropped",
    ),
    (
        "findings.json",
        "the review's findings, each with its draft comment, and its approval verdict with the reason",
    ),
    (
        "overview.json",
        "the review's overview of the change: what it does, where to read closely, and what the review checked and did not",
    ),
    (
        "overview.invalid.json",
        "the overview the review wrote that Whooper dropped, and why",
    ),
    (
        "diff.patch",
        "the pull request's change, git diff <merge base> HEAD",
    ),
    (
        "connections.json",
        "how the code the diff changes connects: the declarations it adds, changes, or removes, and the code that calls them",
    ),
    (
        "test_gaps.json",
        "which changed Go, JavaScript, and TypeScript lines no test ran, as Whooper measured before the review started, and which changed Go lines tests ran still passed their tests when Whooper changed them",
    ),
    (
        "examples.json",
        "what the Go functions the review chose returned for its example inputs at the merge base and at the head, as Whooper ran them",
    ),
    (
        "github.json",
        "the PR's title, body, reviews, and comments, as the review read them",
    ),
    (
        "transcript.jsonl",
        "the review's whole conversation, with every command it ran and that command's output",
    ),
    (
        "transcript.json",
        "the review's whole conversation, with every command it ran and that command's output",
    ),
    (
        "run.json",
        "the work item: the PR, why it is the user's work, and the head and merge-base commits",
    ),
    ("prompt.md", "the instructions the review followed"),
    (
        "drafts.json",
        "the user's choices for the review: which comments go in, edited texts, approve or comment, and the message",
    ),
    ("submit.json", "the review the user submitted to GitHub"),
    (
        "waiting.json",
        "why the review has not run yet: the cause that stopped an earlier review in its round",
    ),
];

/// The files an ask's prompt also lists for a review that did not finish.
const ASK_FAILURE_RECORD: [(&str, &str); 3] = [
    (
        "stderr.log",
        "the error output of the review's agent, which may say why the review did not finish",
    ),
    ("stdout.log", "the rest of the review agent's output"),
    (
        "findings.invalid.json",
        "the findings the review wrote, which did not fit Whooper's findings schema",
    ),
];

/// The findings.json of the review in run as an ask reads it, or None when the review left none
/// Whooper can read: one that holds a list of findings, each with an ID. It need not fit the schema.
pub fn read_findings(run: &Path) -> Option<Value> {
    let result = read_json(&run.join("findings.json"))?;
    let findings = result.get("findings")?.as_array()?;
    if findings
        .iter()
        .all(|f| f.as_object().is_some_and(|f| f.contains_key("id")))
    {
        Some(result)
    } else {
        None
    }
}

/// A finding ID as Python's str() writes it in a message.
fn shown_id(id: &Value) -> String {
    match id {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Null => "None".into(),
        other => other.to_string(),
    }
}

/// The end of an ask's prompt: the review's record files the run may read, earlier asks among them,
/// and for a review that did not finish its agent's output and the findings that did not fit too; the
/// finding the question is about; and the question, not filled in, so braces stay as written.
pub fn ask_facts(run: &Path, question: &str, finding: Option<&str>) -> Result<String> {
    let result = read_findings(run);
    let finished = result.is_some();
    let mut record: Vec<(&str, &str)> = ASK_RECORD.to_vec();
    if !finished {
        record.extend(ASK_FAILURE_RECORD);
    }
    if read_text(&run.join("run.json"))
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .is_none()
    {
        record.retain(|(n, _)| *n != "run.json");
    }
    let mut lines: Vec<String> = vec![
        String::new(),
        String::new(),
        "## The review's record".into(),
        String::new(),
        format!(
            "The review's run directory is {}. You may read these files:",
            run.display()
        ),
        String::new(),
    ];
    for (name, what) in &record {
        if run.join(name).exists() {
            lines.push(format!("- {}: {what}", run.join(name).display()));
        }
    }
    for path in crate::ask::ask_files(run) {
        lines.push(format!(
            "- {}: an earlier question about this review, with its answer",
            path.display()
        ));
    }
    if let Some(id) = finding {
        if !finished && run.join("findings.json").exists() {
            fail!(
                "whooper-ask: the run has no finding {id}; its findings.json holds no findings Whooper can read"
            );
        }
        let findings: Vec<Value> = result
            .as_ref()
            .and_then(|r| r["findings"].as_array().cloned())
            .unwrap_or_default();
        let Some(f) = findings.iter().find(|f| f["id"].as_str() == Some(id)) else {
            let ids: Vec<String> = findings.iter().map(|f| shown_id(&f["id"])).collect();
            fail!(
                "whooper-ask: the run has no finding {id}; its findings are {}",
                if ids.is_empty() {
                    "none".into()
                } else {
                    ids.join(", ")
                }
            )
        };
        let mut name = format!("finding {id}");
        let truthy = |v: &Value| match v {
            Value::Null | Value::Bool(false) => false,
            Value::String(s) => !s.is_empty(),
            Value::Number(n) => n.as_f64() != Some(0.0),
            Value::Array(a) => !a.is_empty(),
            Value::Object(o) => !o.is_empty(),
            _ => true,
        };
        let text = |v: &Value| match v {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        };
        if truthy(&f["title"]) {
            name.push_str(&format!(", {}", text(&f["title"])));
        }
        if truthy(&f["path"]) {
            if truthy(&f["line"]) {
                name.push_str(&format!(" ({}:{})", text(&f["path"]), text(&f["line"])));
            } else {
                name.push_str(&format!(" ({})", text(&f["path"])));
            }
        }
        lines.extend([
            String::new(),
            "## The finding".into(),
            String::new(),
            format!("The question is about {name}, in findings.json."),
        ]);
    }
    lines.extend([
        String::new(),
        "## The question".into(),
        String::new(),
        question.to_string(),
    ]);
    Ok(lines.join("\n") + "\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fill_refuses_a_placeholder_without_a_value() {
        let values = BTreeMap::from([("a", "1".to_string())]);
        assert_eq!(fill("x {{a}} y", &values).unwrap(), "x 1 y");
        assert!(fill("{{b}}", &values).is_err());
        assert_eq!(bot_list(&serde_json::json!([]), "login"), "none");
    }
}
