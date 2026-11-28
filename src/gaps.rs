//! The test-gaps step, test_gaps.json: which of the lines a review's diff adds to the head's Go,
//! JavaScript, and TypeScript code no test ran, measured before the review run starts, while the head's
//! worktree holds the head as committed; and the mutation step, which adds which of the changed Go lines
//! tests ran survived a small change, every test still passing, or were caught.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use regex::Regex;
use serde_json::{Value, json};

use crate::connections::{committed, edited_files, find_go_tool, language_of, which};
use crate::diff::{diff_files, json_lines};
use crate::fsx::{self, Result, limit_text, listed, plural, read_object};
use crate::proc;

const TEST_GAPS_SECONDS: f64 = 600.0;
/// go downloads nothing and runs the Go installed on this Mac, whatever a go.mod asks for.
const GO_OFFLINE: [(&str, &str); 2] = [("GOPROXY", "off"), ("GOTOOLCHAIN", "local")];
/// CI=true keeps Vitest and Jest from asking questions or writing new snapshots in the head's worktree,
/// and npm's offline setting keeps a package manager the tests start from installing anything.
const NODE_OFFLINE: [(&str, &str); 2] = [("CI", "true"), ("npm_config_offline", "true")];
/// The test runners a package's JavaScript and TypeScript tests run with, by the name a package.json
/// gives each, with the name a person reads.
const NODE_RUNNERS: [(&str, &str); 2] = [("vitest", "Vitest"), ("jest", "Jest")];
const CHECKED: &str =
    "A line that ran is not always checked: a test can run a line without checking what it does.";
const OWN_TESTS: &str = "Only the tests of the changed packages ran, so a test in another package that runs these lines does not count.";

/// The step's limit passed.
pub struct TimeLimit;

/// A word as a shell reads it, quoted as Python's shlex.quote quotes it.
pub fn quote(word: &str) -> String {
    if word.is_empty() {
        return "''".into();
    }
    if word
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || "_@%+=:,./-".contains(c))
    {
        return word.into();
    }
    format!("'{}'", word.replace('\'', "'\"'\"'"))
}

pub fn shell_join(words: &[String]) -> String {
    words.iter().map(|w| quote(w)).collect::<Vec<_>>().join(" ")
}

/// command, words a step runs in folder of the head's worktree, as a person would type them at the
/// worktree's root.
pub fn shown_command(folder: &str, command: &str) -> String {
    if folder.is_empty() || folder == "." {
        command.into()
    } else {
        format!("(cd {} && {command})", quote(folder))
    }
}

/// A runner of commands within what is left of a step's limit.
pub struct Limit {
    deadline: Instant,
}

impl Limit {
    pub fn new(seconds: f64) -> Limit {
        Limit {
            deadline: Instant::now() + Duration::from_secs_f64(seconds),
        }
    }

    pub fn left(&self) -> Duration {
        self.deadline.saturating_duration_since(Instant::now())
    }

    /// Runs program with args in folder with env added, in a session of its own, and returns its exit
    /// code, output, and error output, or TimeLimit once the step's limit passed, before it starts the
    /// command if none of it is left.
    pub fn run(
        &self,
        program: &Path,
        args: &[String],
        folder: &Path,
        env: &[(&str, &str)],
    ) -> std::result::Result<(i32, String, String), TimeLimit> {
        let left = self.left();
        if left.is_zero() {
            return Err(TimeLimit);
        }
        let mut command = Command::new(program);
        command.args(args).current_dir(folder);
        for (k, v) in env {
            command.env(k, v);
        }
        match proc::run_captured(&mut command, Some(left)) {
            Ok(c) => match c.code {
                Some(code) => Ok((code, c.out, c.err)),
                None => Err(TimeLimit),
            },
            Err(e) => Ok((127, String::new(), e.to_string())),
        }
    }
}

/// The first line of the error that stopped go from building, from go test -json's events, or else from
/// its error output.
pub fn build_error(events: &[Value], err: &str) -> String {
    let mut lines: Vec<String> = events
        .iter()
        .filter(|e| e["Action"] == "build-output")
        .map(|e| e["Output"].as_str().unwrap_or("").trim().to_string())
        .collect();
    lines.extend(err.lines().map(str::to_string));
    lines
        .iter()
        .find(|l| !l.trim().is_empty() && !l.starts_with("# "))
        .map(|l| l.trim().to_string())
        .unwrap_or_else(|| "go printed no error".into())
}

/// The first line of a panic or fatal error the tests of package printed, or None.
pub fn first_panic(events: &[Value], package: &Value) -> Option<String> {
    events
        .iter()
        .find(|e| {
            &e["Package"] == package
                && e["Action"] == "output"
                && e["Output"]
                    .as_str()
                    .is_some_and(|o| o.starts_with("panic: ") || o.starts_with("fatal error: "))
        })
        .map(|e| e["Output"].as_str().unwrap_or("").trim().to_string())
}

type Block = (String, i64, i64, i64, i64);

/// The blocks of a Go coverage profile, each with the highest count the profile gives it, since go test
/// writes a block once for each test binary that covers its package.
pub fn coverage_counts(profile: &str) -> HashMap<Block, i64> {
    let re = Regex::new(r"^(.+):(\d+)\.(\d+),(\d+)\.(\d+) \d+ (\d+)$").unwrap();
    let mut counts = HashMap::new();
    for line in profile.lines() {
        if let Some(m) = re.captures(line) {
            let n = |i: usize| m[i].parse::<i64>().unwrap_or(0);
            let key = (m[1].to_string(), n(2), n(3), n(4), n(5));
            let count = n(6);
            let entry = counts.entry(key).or_insert(0);
            *entry = (*entry).max(count);
        }
    }
    counts
}

/// The lines an LCov report lists for each source file, by its SF path, each with the highest count the
/// report gives it. A DA line may end in a checksum; only a newline ends a line.
pub fn lcov_counts(report: &str) -> Vec<(String, HashMap<i64, i64>)> {
    let da = Regex::new(r"^DA:(\d+),(\d+)(?:,.*)?$").unwrap();
    let mut counts: Vec<(String, HashMap<i64, i64>)> = vec![];
    let mut current: Option<usize> = None;
    for line in report.split('\n') {
        let line = line.strip_suffix('\r').unwrap_or(line);
        if let Some(source) = line.strip_prefix("SF:") {
            current = Some(match counts.iter().position(|(s, _)| s == source) {
                Some(at) => at,
                None => {
                    counts.push((source.to_string(), HashMap::new()));
                    counts.len() - 1
                }
            });
        } else if line == "end_of_record" {
            current = None;
        } else if let (Some(at), Some(m)) = (current, da.captures(line)) {
            let (number, count) = (m[1].parse().unwrap_or(0), m[2].parse().unwrap_or(0));
            let entry = counts[at].1.entry(number).or_insert(0);
            *entry = (*entry).max(count);
        }
    }
    counts
}

/// path as a glob that matches only that path, for Vitest's and Jest's coverage settings.
pub fn glob_escape(path: &str) -> String {
    let mut out = String::new();
    for c in path.chars() {
        if "\\*?[]{}()!+@".contains(c) {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// The arguments that run a package's tests once with runner with coverage of files, globs relative to
/// the package, as an LCov report in folder: those a person reads in the tool, and the rest. Each also
/// adds a file no test loads, with every line not run. Vitest writes no report when a test fails unless
/// told to; Jest runs without watchman, which would stay running after it.
fn node_arguments(runner: &str, files: &[String], folder: &Path) -> (Vec<String>, Vec<String>) {
    if runner == "vitest" {
        let mut shown = vec![
            "run".into(),
            "--coverage".into(),
            "--coverage.reporter=lcov".into(),
        ];
        shown.extend(files.iter().map(|f| format!("--coverage.include={f}")));
        (
            shown,
            vec![
                format!("--coverage.reportsDirectory={}", folder.display()),
                "--coverage.reportOnFailure".into(),
            ],
        )
    } else {
        let mut shown = vec!["--coverage".into(), "--coverageReporters=lcov".into()];
        shown.extend(files.iter().map(|f| format!("--collectCoverageFrom={f}")));
        (
            shown,
            vec![
                format!("--coverageDirectory={}", folder.display()),
                "--no-watchman".into(),
            ],
        )
    }
}

fn dirname(path: &str) -> &str {
    path.rfind('/').map(|i| &path[..i]).unwrap_or("")
}

fn basename(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

type Changed = Vec<(String, Vec<i64>)>;

/// The lines patch adds to the code the test-gaps step measures, in commit, which the worktree head
/// holds, as the Go files and the JavaScript and TypeScript files each by path: lines that are not blank
/// or comments, of files that are not tests, and for Go not generated or in testdata either, and for
/// JavaScript and TypeScript not declaration files.
pub fn changed_code(head: &Path, commit: &str, patch: &str) -> (Changed, Changed) {
    let not_measured =
        Regex::new(r"(?:^|/)(?:__tests__|__mocks__)/|\.(?:test|spec)\.[cm]?[jt]sx?$|\.d\.[cm]?ts$")
            .unwrap();
    let package = Regex::new(r"(?m)^package\s").unwrap();
    let generated = Regex::new(r"(?m)^// Code generated .* DO NOT EDIT\.$").unwrap();
    let (mut go, mut js) = (vec![], vec![]);
    for f in diff_files(patch) {
        let language = language_of(&f.path);
        if f.added.is_empty() || !matches!(language, Some("go" | "javascript")) {
            continue;
        }
        let is_go = language == Some("go");
        let skip = if is_go {
            f.path.ends_with("_test.go") || f.path.split('/').any(|p| p == "testdata")
        } else {
            not_measured.is_match(&f.path)
        };
        if skip {
            continue;
        }
        let Some(text) = committed(head, commit, &f.path) else {
            continue;
        };
        if is_go {
            let top = match package.find(&text) {
                Some(m) => &text[..m.start()],
                None => &text[..],
            };
            if generated.is_match(top) {
                continue;
            }
        }
        let lines: Vec<&str> = text.split('\n').collect();
        let comment: &[&str] = if is_go { &["//"] } else { &["//", "/*", "*"] };
        let code: Vec<i64> = f
            .added
            .iter()
            .copied()
            .filter(|&n| {
                n >= 1 && n as usize <= lines.len() && {
                    let line = lines[n as usize - 1].trim();
                    !line.is_empty() && !comment.iter().any(|c| line.starts_with(c))
                }
            })
            .collect();
        if !code.is_empty() {
            if is_go { &mut go } else { &mut js }.push((f.path.clone(), code));
        }
    }
    (go, js)
}

/// What one part of test_gaps.json measured.
struct Part {
    tools: Vec<String>,
    reason: Option<String>,
    missed: Option<String>,
    lines: Vec<(String, i64)>,
    ran: usize,
    limits: Vec<String>,
}

fn missed_part(tools: Vec<String>, reason: String, missed: String) -> Part {
    Part {
        tools,
        reason: Some(reason),
        missed: Some(missed),
        lines: vec![],
        ran: 0,
        limits: vec![],
    }
}

fn relative(path: &Path, base: &Path) -> String {
    match path.strip_prefix(base) {
        Ok(p) if p.as_os_str().is_empty() => ".".into(),
        Ok(p) => p.to_string_lossy().into_owned(),
        Err(_) => path.to_string_lossy().into_owned(),
    }
}

fn go_env() -> Vec<(&'static str, &'static str)> {
    GO_OFFLINE.to_vec()
}

/// What the test-gaps step measures of the changed Go lines: go test runs the changed packages' own
/// tests with coverage of all of them, from the folder of their module, or of its workspace, so a block
/// counts as run when the tests of any of those packages ran it. A changed file the build on this Mac
/// leaves out is not measured, and limits says so. Nothing is measured, and reason says why, without the
/// go command, when the head does not build, when a test binary ends without recording what it ran, as
/// when a test panics, and at the time limit; a test that fails still measures, and limits names it.
fn go_coverage(head: &Path, changed: &Changed, limit: &Limit, seconds: f64) -> Part {
    let mut tools: Vec<String> = vec![];
    let missed = |tools: &Vec<String>, reason: String| {
        let text = format!("Whooper did not measure the changed Go lines. {reason}");
        missed_part(tools.clone(), reason, text)
    };
    let Some(go) = which("go") else {
        return missed(&tools, "The go command is not on PATH.".into());
    };
    let go_run = |folder: &Path, args: &[&str]| {
        let args: Vec<String> = args.iter().map(|a| a.to_string()).collect();
        limit.run(&go, &args, folder, &go_env())
    };
    let folders: Vec<(String, PathBuf)> = changed
        .iter()
        .map(|(p, _)| (p.clone(), head.join(dirname(p))))
        .collect();
    let mut roots: Vec<(PathBuf, Vec<PathBuf>)> = vec![];
    let (mut unreachable, mut left_out, mut failed) = (vec![], vec![], vec![]);
    let mut keys: HashMap<String, String> = HashMap::new();
    let mut counts: HashMap<Block, i64> = HashMap::new();
    let body = (|| -> std::result::Result<Option<Part>, TimeLimit> {
        let mut seen = HashSet::new();
        for (_, folder) in &folders {
            if !seen.insert(folder.clone()) {
                continue;
            }
            let (_, out, _) = go_run(folder, &["env", "GOWORK", "GOMOD"])?;
            let mut lines = out.split('\n');
            let (work, module) = (lines.next().unwrap_or(""), lines.next().unwrap_or(""));
            let file = if !work.is_empty() && work != "off" {
                Some(work)
            } else if !module.is_empty() && module != "/dev/null" {
                Some(module)
            } else {
                None
            };
            if let Some(file) = file {
                let root = Path::new(file)
                    .parent()
                    .unwrap_or(Path::new("/"))
                    .to_path_buf();
                match roots.iter_mut().find(|(r, _)| *r == root) {
                    Some((_, members)) => members.push(folder.clone()),
                    None => roots.push((root, vec![folder.clone()])),
                }
            }
        }
        for (path, folder) in &folders {
            if !roots.iter().any(|(_, m)| m.contains(folder)) {
                unreachable.push(path.clone());
            }
        }
        for (root, members) in &roots {
            let pattern = |folder: &PathBuf| {
                if folder == root {
                    ".".to_string()
                } else {
                    format!("./{}", relative(folder, root))
                }
            };
            let mut args = vec!["list", "-e", "-json=ImportPath,Dir,GoFiles,CgoFiles"];
            let patterns: Vec<String> = members.iter().map(pattern).collect();
            args.extend(patterns.iter().map(String::as_str));
            let (code, out, err) = go_run(root, &args)?;
            if code != 0 {
                return Ok(Some(missed(
                    &tools,
                    format!(
                        "go list did not list the changed packages: {}",
                        build_error(&[], &err)
                    ),
                )));
            }
            let mut packages: HashMap<PathBuf, Value> = HashMap::new();
            for package in serde_json::Deserializer::from_str(&out)
                .into_iter::<Value>()
                .flatten()
            {
                let dir = PathBuf::from(package["Dir"].as_str().unwrap_or(""));
                packages.insert(fs::canonicalize(&dir).unwrap_or(dir), package);
            }
            let mut tested = std::collections::BTreeSet::new();
            for (path, folder) in &folders {
                if !members.contains(folder) {
                    continue;
                }
                let canonical = fs::canonicalize(folder).unwrap_or(folder.clone());
                let package = packages.get(&canonical).cloned().unwrap_or(Value::Null);
                let name = basename(path);
                let files: Vec<&str> = ["GoFiles", "CgoFiles"]
                    .iter()
                    .flat_map(|k| package[k].as_array().into_iter().flatten())
                    .filter_map(Value::as_str)
                    .collect();
                if files.contains(&name) {
                    keys.insert(
                        path.clone(),
                        format!("{}/{name}", package["ImportPath"].as_str().unwrap_or("")),
                    );
                    tested.insert(pattern(folder));
                } else {
                    left_out.push(path.clone());
                }
            }
            if tested.is_empty() {
                continue;
            }
            let tested: Vec<String> = tested.into_iter().collect();
            let coverpkg = format!("-coverpkg={}", tested.join(","));
            let mut shown = vec![
                "go".to_string(),
                "test".into(),
                "-count=1".into(),
                coverpkg.clone(),
            ];
            shown.extend(tested.iter().cloned());
            tools.push(shown_command(
                &relative(root, head),
                &format!("GOPROXY=off GOTOOLCHAIN=local {}", shell_join(&shown)),
            ));
            let scratch = tempfile::tempdir().map_err(|_| TimeLimit)?;
            let profile_path = scratch.path().join("coverage.out");
            let mut args: Vec<String> =
                vec!["test".into(), "-json".into(), "-count=1".into(), coverpkg];
            args.push(format!("-coverprofile={}", profile_path.display()));
            args.extend(tested.iter().cloned());
            let (code, out, err) = limit.run(&go, &args, root, &go_env())?;
            let profile = fsx::read_text(&profile_path).unwrap_or_default();
            let events = json_lines(&out);
            if events.is_empty() && code != 0 {
                return Ok(Some(missed(
                    &tools,
                    format!("go test did not run: {}", build_error(&[], &err)),
                )));
            }
            if events.iter().any(|e| {
                e["Action"] == "build-fail"
                    || !matches!(
                        e.get("FailedBuild"),
                        None | Some(Value::Null) | Some(Value::Bool(false))
                    ) && e["FailedBuild"] != ""
            }) {
                return Ok(Some(missed(
                    &tools,
                    format!(
                        "The Go code at the head does not build: {}",
                        build_error(&events, &err)
                    ),
                )));
            }
            let no_test = |e: &Value| e["Test"].as_str().is_none_or(str::is_empty);
            let recorded: Vec<&Value> = events
                .iter()
                .filter(|e| {
                    e["Action"] == "output"
                        && no_test(e)
                        && e["Output"]
                            .as_str()
                            .is_some_and(|o| o.contains("coverage: "))
                })
                .map(|e| &e["Package"])
                .collect();
            if let Some(crashed) = events
                .iter()
                .find(|e| e["Action"] == "fail" && no_test(e) && !recorded.contains(&&e["Package"]))
            {
                let panic = first_panic(&events, &crashed["Package"]);
                return Ok(Some(missed(
                    &tools,
                    format!(
                        "The tests of {} stopped before Go recorded what they ran, as when a test panics{}.",
                        crashed["Package"].as_str().unwrap_or(""),
                        panic.map(|p| format!(": {p}")).unwrap_or_default()
                    ),
                )));
            }
            for e in &events {
                if e["Action"] == "fail"
                    && let Some(test) = e["Test"]
                        .as_str()
                        .filter(|t| !t.is_empty() && !t.contains('/'))
                {
                    failed.push(format!("{test} in {}", e["Package"].as_str().unwrap_or("")));
                }
            }
            for (block, count) in coverage_counts(&profile) {
                let entry = counts.entry(block).or_insert(0);
                *entry = (*entry).max(count);
            }
        }
        Ok(None)
    })();
    match body {
        Err(TimeLimit) => {
            return missed(
                &tools,
                format!(
                    "The tests ran past Whooper's {} limit.",
                    limit_text(seconds)
                ),
            );
        }
        Ok(Some(part)) => return part,
        Ok(None) => {}
    }
    if tools.is_empty() {
        let all: Vec<String> = left_out.iter().chain(unreachable.iter()).cloned().collect();
        return missed(
            &tools,
            format!(
                "Whooper found no package go test can run for the changed Go files: {}.",
                listed(&all, 5)
            ),
        );
    }
    // Each block's lines: an end at column 1 holds none of its line.
    let mut spans: HashMap<String, Vec<(i64, i64, i64)>> = HashMap::new();
    for ((file, start, _, end, end_column), count) in &counts {
        spans.entry(file.clone()).or_default().push((
            *start,
            if *end_column > 1 { *end } else { end - 1 },
            *count,
        ));
    }
    let (mut ran, mut not_run) = (0, vec![]);
    for (path, lines) in changed {
        let Some(key) = keys.get(path) else { continue };
        for &n in lines {
            let found: Vec<i64> = spans
                .get(key)
                .into_iter()
                .flatten()
                .filter(|(s, e, _)| *s <= n && n <= *e)
                .map(|(_, _, c)| *c)
                .collect();
            if let Some(&most) = found.iter().max() {
                if most > 0 {
                    ran += 1;
                } else {
                    not_run.push((path.clone(), n));
                }
            }
        }
    }
    let mut limits = vec![
        "Go counts a line as run when any part of it ran: in `a && b`, the line counts as run even when b never ran.".to_string(),
        CHECKED.into(),
        OWN_TESTS.into(),
    ];
    if !failed.is_empty() {
        let they = if failed.len() == 1 { "it" } else { "they" };
        limits.push(format!(
            "{} failed: {}. Go recorded the lines {they} ran before {they} failed, so they count as run.",
            plural(failed.len(), "test", "tests"),
            listed(&failed, 5)
        ));
    }
    if !left_out.is_empty() {
        limits.push(format!(
            "The build on this Mac leaves out {}, so Whooper did not measure {}: {}.",
            plural(left_out.len(), "changed Go file", "changed Go files"),
            if left_out.len() == 1 { "it" } else { "them" },
            listed(&left_out, 5)
        ));
    }
    if !unreachable.is_empty() {
        limits.push(format!(
            "Whooper found no Go module for {}, so it did not measure {}: {}.",
            plural(unreachable.len(), "changed Go file", "changed Go files"),
            if unreachable.len() == 1 { "it" } else { "them" },
            listed(&unreachable, 5)
        ));
    }
    Part {
        tools,
        reason: None,
        missed: None,
        lines: not_run,
        ran,
        limits,
    }
}

/// path made absolute and without . and .. parts, and with its links resolved where it exists.
fn real(path: &Path) -> PathBuf {
    if let Ok(p) = fs::canonicalize(path) {
        return p;
    }
    let mut out = PathBuf::new();
    for c in path.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
}

/// A path relative to base, as os.path.relpath writes it.
fn relpath(path: &Path, base: &Path) -> String {
    let (p, b): (Vec<_>, Vec<_>) = (path.components().collect(), base.components().collect());
    let common = p.iter().zip(&b).take_while(|(x, y)| x == y).count();
    let mut parts: Vec<String> = vec!["..".into(); b.len() - common];
    parts.extend(
        p[common..]
            .iter()
            .map(|c| c.as_os_str().to_string_lossy().into_owned()),
    );
    if parts.is_empty() {
        ".".into()
    } else {
        parts.join("/")
    }
}

/// What the test-gaps step measures of the changed JavaScript and TypeScript lines, one part for each
/// package: the folder of the package.json nearest above a file. A package's tests run once when its
/// package.json names Vitest or Jest, in its test script first and then in its dependencies, and its
/// dependencies are installed, which in the head's worktree only the repo's setup script does, with
/// coverage of the package's changed files as an LCov report.
fn node_coverage(head: &Path, changed: &Changed, limit: &Limit, seconds: f64) -> Vec<Part> {
    let mut packages: Vec<(Option<String>, Vec<String>)> = vec![];
    for (path, _) in changed {
        let mut folder = Some(dirname(path).to_string());
        while let Some(f) = &folder {
            if head.join(f).join("package.json").is_file() {
                break;
            }
            folder = if f.is_empty() {
                None
            } else {
                Some(dirname(f).to_string())
            };
        }
        match packages.iter_mut().find(|(f, _)| *f == folder) {
            Some((_, paths)) => paths.push(path.clone()),
            None => packages.push((folder, vec![path.clone()])),
        }
    }
    let ansi = Regex::new(r"\x1b\[[0-9;]*m").unwrap();
    let lines_of: HashMap<&String, &Vec<i64>> = changed.iter().map(|(p, l)| (p, l)).collect();
    let mut parts = vec![];
    for (folder, paths) in packages {
        let files = listed(&paths, 5);
        let count = plural(paths.len(), "changed file", "changed files");
        let them = if paths.len() == 1 { "it" } else { "them" };
        let mut tools: Vec<String> = vec![];
        let missed = |tools: &Vec<String>, why: &str| {
            let text = match &folder {
                None => format!(
                    "Whooper did not measure {count}, since no package.json is above {them}: {files}."
                ),
                Some(f) => format!(
                    "Whooper did not measure {count} in {}, since {why}: {files}.",
                    if f.is_empty() { "the root package" } else { f }
                ),
            };
            missed_part(tools.clone(), text.clone(), text)
        };
        let Some(folder_name) = folder.clone() else {
            parts.push(missed(&tools, ""));
            continue;
        };
        let top = head.join(&folder_name);
        let manifest = match fs::read_to_string(top.join("package.json")) {
            Ok(text) => match serde_json::from_str::<Value>(&text) {
                Ok(v) => v,
                Err(e) => {
                    parts.push(missed(
                        &tools,
                        &format!("its package.json cannot be read: {e}"),
                    ));
                    continue;
                }
            },
            Err(e) => {
                parts.push(missed(
                    &tools,
                    &format!("its package.json cannot be read: {e}"),
                ));
                continue;
            }
        };
        let script = manifest["scripts"]["test"].as_str().unwrap_or("");
        let depends: HashSet<&str> = ["dependencies", "devDependencies"]
            .iter()
            .filter_map(|k| manifest[k].as_object())
            .flat_map(|o| o.keys().map(String::as_str))
            .collect();
        let runner = NODE_RUNNERS
            .iter()
            .find(|(name, _)| {
                Regex::new(&format!(r"\b{name}\b"))
                    .unwrap()
                    .is_match(script)
            })
            .or_else(|| NODE_RUNNERS.iter().find(|(name, _)| depends.contains(name)));
        let Some((runner, name)) = runner else {
            parts.push(missed(
                &tools,
                "its package.json names neither Vitest nor Jest",
            ));
            continue;
        };
        let program = top
            .ancestors()
            .filter(|f| f.starts_with(head))
            .find_map(|f| {
                use std::os::unix::fs::PermissionsExt;
                let bin = f.join("node_modules/.bin").join(runner);
                fs::metadata(&bin)
                    .is_ok_and(|m| m.permissions().mode() & 0o111 != 0)
                    .then_some(bin)
            });
        let Some(program) = program else {
            parts.push(missed(&tools, &format!("its dependencies are not installed: Whooper found no node_modules/.bin/{runner} there or in a folder above it, which the repo's setup script would install")));
            continue;
        };
        if which("node").is_none() {
            parts.push(missed(&tools, "the node command is not on PATH"));
            continue;
        }
        let Ok(scratch) = tempfile::tempdir() else {
            parts.push(missed(&tools, "Whooper could not make a temporary folder"));
            continue;
        };
        let globs: Vec<String> = paths
            .iter()
            .map(|p| {
                let prefix = format!("{folder_name}/");
                glob_escape(if folder_name.is_empty() {
                    p
                } else {
                    p.strip_prefix(&prefix).unwrap_or(p)
                })
            })
            .collect();
        let (shown, rest) = node_arguments(runner, &globs, scratch.path());
        let mut words = vec![relpath(&program, &top)];
        words.extend(shown.iter().cloned());
        tools.push(shown_command(
            &folder_name,
            &format!("CI=true npm_config_offline=true {}", shell_join(&words)),
        ));
        let args: Vec<String> = shown.iter().chain(rest.iter()).cloned().collect();
        let (code, out, err) = match limit.run(&program, &args, &top, &NODE_OFFLINE) {
            Ok(r) => r,
            Err(TimeLimit) => {
                parts.push(missed(
                    &tools,
                    &format!("its tests ran past Whooper's {} limit", limit_text(seconds)),
                ));
                continue;
            }
        };
        let Some(report) = fsx::read_text(&scratch.path().join("lcov.info")) else {
            let printed = ansi.replace_all(&format!("{err}\n{out}"), "").into_owned();
            let first = printed
                .split('\n')
                .find(|l| !l.trim().is_empty())
                .map(|l| l.trim().to_string())
                .unwrap_or_else(|| format!("it exited with code {code}"));
            parts.push(missed(
                &tools,
                &format!("{name} wrote no coverage report: {first}"),
            ));
            continue;
        };
        let mut counts: HashMap<String, HashMap<i64, i64>> = HashMap::new();
        for (source, lines) in lcov_counts(&report) {
            let file = real(&top.join(&source));
            if let Ok(rel) = file.strip_prefix(head) {
                counts.insert(rel.to_string_lossy().into_owned(), lines);
            }
        }
        let absent: Vec<String> = paths
            .iter()
            .filter(|p| !counts.contains_key(*p))
            .cloned()
            .collect();
        if absent.len() == paths.len() {
            parts.push(missed(
                &tools,
                &format!("{name}'s report covers none of {them}, as when the package's coverage settings leave {them} out"),
            ));
            continue;
        }
        let (mut ran, mut not_run) = (0, vec![]);
        for path in &paths {
            let Some(lines) = counts.get(path) else {
                continue;
            };
            for n in lines_of[path].iter() {
                if let Some(&c) = lines.get(n) {
                    if c > 0 {
                        ran += 1;
                    } else {
                        not_run.push((path.clone(), *n));
                    }
                }
            }
        }
        let where_ = if folder_name.is_empty() {
            "the root package".to_string()
        } else {
            folder_name.clone()
        };
        let mut limits = vec![
            "Vitest and Jest count a line as run when a statement that starts on it ran: in `a && b`, the line counts as run even when b never ran, and the other lines of a statement that spans several lines do not count.".to_string(),
            CHECKED.into(),
            OWN_TESTS.into(),
        ];
        if code != 0 {
            limits.push(format!("{name} exited with code {code} in {where_}, as it does when a test fails; the lines the tests ran count as run."));
        }
        if !absent.is_empty() {
            let it = if absent.len() == 1 { "it" } else { "them" };
            limits.push(format!(
                "{name}'s report in {where_} covers none of {}, as when the package's coverage settings leave {it} out, so Whooper did not measure {it}: {}.",
                plural(absent.len(), "changed file", "changed files"),
                listed(&absent, 5)
            ));
        }
        parts.push(Part {
            tools,
            reason: None,
            missed: None,
            lines: not_run,
            ran,
            limits,
        });
    }
    parts
}

fn order_of(patch: &str) -> HashMap<String, usize> {
    let mut order = HashMap::new();
    for (i, f) in diff_files(patch).into_iter().enumerate() {
        order.entry(f.path).or_insert(i);
    }
    order
}

/// whooper step test-gaps: writes test_gaps.json in run. The changed Go lines and each package's
/// changed JavaScript and TypeScript lines are each a part, measured in that order within one limit, and
/// the file holds them all. It is measured when a part is, and otherwise not, with the first part's
/// reason. A PR whose diff adds no such line gets no file.
pub fn test_gaps(run: &Path) -> Result<()> {
    test_gaps_within(run, TEST_GAPS_SECONDS)
}

pub fn test_gaps_within(run: &Path, seconds: f64) -> Result<()> {
    let item = read_object(&run.join("run.json")).unwrap_or_default();
    let patch = fsx::read_text(&run.join("diff.patch")).unwrap_or_default();
    let head = fs::canonicalize(run)?.join("head");
    let (go, node) = changed_code(&head, fsx::str_of(&item, "head_sha").unwrap_or(""), &patch);
    if go.is_empty() && node.is_empty() {
        return Ok(());
    }
    let limit = Limit::new(seconds);
    let mut parts = vec![];
    if !go.is_empty() {
        parts.push(go_coverage(&head, &go, &limit, seconds));
    }
    parts.extend(node_coverage(&head, &node, &limit, seconds));
    let measured: Vec<&Part> = parts.iter().filter(|p| p.reason.is_none()).collect();
    let missed: Vec<&Part> = parts.iter().filter(|p| p.reason.is_some()).collect();
    let order = order_of(&patch);
    let mut lines: Vec<(String, i64)> = measured.iter().flat_map(|p| p.lines.clone()).collect();
    lines.sort_by_key(|(p, n)| (order.get(p).copied().unwrap_or(usize::MAX), *n));
    let ran: usize = measured.iter().map(|p| p.ran).sum();
    let mut limits: Vec<String> = measured.iter().flat_map(|p| p.limits.clone()).collect();
    let skip = if measured.is_empty() { 1 } else { 0 };
    limits.extend(missed.iter().skip(skip).filter_map(|p| p.missed.clone()));
    let others: Vec<String> = diff_files(&patch)
        .into_iter()
        .filter(|f| {
            !f.added.is_empty() && !matches!(language_of(&f.path), None | Some("go" | "javascript"))
        })
        .map(|f| f.path)
        .collect();
    if !others.is_empty() {
        limits.push(format!(
            "Whooper measures only Go, JavaScript, and TypeScript code so far, so it left out {}: {}.",
            plural(others.len(), "changed file in another language", "changed files in other languages"),
            listed(&others, 5)
        ));
    }
    let mut unique = vec![];
    for l in limits {
        if !unique.contains(&l) {
            unique.push(l);
        }
    }
    let tools: Vec<String> = parts.iter().flat_map(|p| p.tools.clone()).collect();
    let tool = if tools.is_empty() {
        if go.is_empty() { "" } else { "go test" }.to_string()
    } else {
        tools.join("; ")
    };
    let gaps = json!({
        "measured": !measured.is_empty(),
        "reason": if measured.is_empty() { json!(missed[0].reason) } else { Value::Null },
        "tool": tool,
        "lines": lines.iter().map(|(p, n)| json!({"path": p, "line": n, "status": "not_run"})).collect::<Vec<_>>(),
        "summary": {"changed_lines": ran + lines.len(), "run": ran, "not_run": lines.len()},
        "limits": unique,
    });
    fsx::write_file(&run.join("test_gaps.json"), &fsx::pretty(&gaps), false)?;
    Ok(())
}

const MUTATION_SECONDS: f64 = 900.0;
/// test_gaps.json shows at most this many survived or caught lines a file.
const MUTATION_LINES_PER_FILE: usize = 7;
/// gremlins gives each changed line's tests its coverage run's time times this: each change compiles its
/// package again in a copy, so gremlins' own 3 ran out on every change in a small package, and 10 did not.
const MUTATION_TIMEOUT_COEFFICIENT: u32 = 10;
const MUTATION_WORKERS: u32 = 2;

/// What gremlins 0.6.0 changes each operator to, by the name its report gives each kind of change.
fn mutations(kind: &str) -> &'static [(&'static str, &'static str)] {
    match kind {
        "ARITHMETIC_BASE" => &[("+", "-"), ("-", "+"), ("*", "/"), ("/", "*"), ("%", "*")],
        "CONDITIONALS_BOUNDARY" => &[(">", ">="), (">=", ">"), ("<", "<="), ("<=", "<")],
        "CONDITIONALS_NEGATION" => &[
            ("==", "!="),
            ("!=", "=="),
            (">", "<="),
            (">=", "<"),
            ("<", ">="),
            ("<=", ">"),
        ],
        "INCREMENT_DECREMENT" => &[("++", "--"), ("--", "++")],
        "INVERT_NEGATIVES" => &[("-", "+")],
        _ => &[],
    }
}

/// git settings for the git diff gremlins runs to find the changed lines, which win over the run's own:
/// paths relative to the folder it starts in, no lines around each change, since gremlins takes a hunk's
/// lines as one range from its first added line, and no color, which gremlins cannot read; the rest keep
/// the user's git config from changing the patch's shape.
const MUTATION_GIT: [(&str, &str); 20] = [
    ("diff.relative", "true"),
    ("diff.context", "0"),
    ("color.diff", "false"),
    ("diff.interHunkContext", "0"),
    ("diff.algorithm", "myers"),
    ("diff.indentHeuristic", "true"),
    ("diff.suppressBlankEmpty", "false"),
    ("diff.renames", "true"),
    ("diff.renameLimit", "1000"),
    ("diff.orderFile", "/dev/null"),
    ("core.quotePath", "true"),
    ("core.bigFileThreshold", "512m"),
    ("core.attributesFile", "/dev/null"),
    ("diff.ignoreSubmodules", "none"),
    ("diff.submodule", "short"),
    ("diff.noprefix", "false"),
    ("diff.srcPrefix", "a/"),
    ("diff.dstPrefix", "b/"),
    ("diff.mnemonicPrefix", "false"),
    ("core.abbrev", "12"),
];

/// What gremlins changed at line and column, a byte offset counted from 1, of text, in a change of kind,
/// in words, such as `>` to `>=`, or the kind's name when Whooper does not know what it changes there.
pub fn mutation_change(text: &str, line: i64, column: i64, kind: &str) -> String {
    let lines: Vec<&str> = text.split('\n').collect();
    let at = if line > 0 && line as usize <= lines.len() {
        let bytes = lines[line as usize - 1].as_bytes();
        let from = ((column - 1).max(0) as usize).min(bytes.len());
        String::from_utf8_lossy(&bytes[from..]).into_owned()
    } else {
        String::new()
    };
    let mut table: Vec<&(&str, &str)> = mutations(kind).iter().collect();
    table.sort_by_key(|(op, _)| std::cmp::Reverse(op.len()));
    match table.iter().find(|(op, _)| at.starts_with(op)) {
        Some((op, to)) => format!("`{op}` to `{to}`"),
        None => format!("gremlins's {kind} change at column {column}"),
    }
}

/// whooper step mutation: adds to test_gaps.json which of the changed Go lines tests ran gremlins
/// changed in one small way with the tests of the line's package passing all the same, survived, or one
/// failing, caught. gremlins runs once in the folder of each changed Go file, but for one inside another
/// such folder, with --diff at the merge base, so it changes only lines the diff adds and its own
/// coverage run finds a test runs; first the Go files the run changed or added in the head's worktree get
/// their committed text back, or go. A line shows one change, a surviving one first, and a file at most
/// MUTATION_LINES_PER_FILE lines. Without a test_gaps.json that measured lines, or without changed Go
/// lines, it changes nothing.
pub fn mutation(run: &Path) -> Result<()> {
    mutation_within(run, MUTATION_SECONDS)
}

pub fn mutation_within(run: &Path, seconds: f64) -> Result<()> {
    let Some(mut gaps) = read_object(&run.join("test_gaps.json")) else {
        return Ok(());
    };
    if gaps.get("measured") != Some(&Value::Bool(true)) {
        return Ok(());
    }
    let item = read_object(&run.join("run.json")).unwrap_or_default();
    let patch = fsx::read_text(&run.join("diff.patch")).unwrap_or_default();
    let head = fs::canonicalize(run)?.join("head");
    let commit = fsx::str_of(&item, "head_sha").unwrap_or("").to_string();
    let merge_base = fsx::str_of(&item, "merge_base").unwrap_or("").to_string();
    let (changed, node_changed) = changed_code(&head, &commit, &patch);
    if changed.is_empty() {
        return Ok(());
    }
    let (program, missing) = find_go_tool("gremlins");
    let mut folders: Vec<String> = changed
        .iter()
        .map(|(p, _)| dirname(p).to_string())
        .collect();
    folders.sort();
    folders.dedup();
    let all = folders.clone();
    folders.retain(|f| {
        !all.iter()
            .any(|o| o != f && (o.is_empty() || f.starts_with(&format!("{o}/"))))
    });
    let (mut tools, mut problems, mut results) = (vec![], vec![], vec![]);
    if let Some(program) = &program {
        for edited in edited_files(&head, &[".go"]).unwrap_or_default() {
            let out = Command::new("git")
                .arg("-C")
                .arg(&head)
                .args([
                    "--no-lazy-fetch",
                    "cat-file",
                    "blob",
                    &format!("{commit}:{edited}"),
                ])
                .output()?;
            if out.status.success() {
                fs::write(head.join(&edited), out.stdout)?;
            } else {
                let _ = fs::remove_file(head.join(&edited));
            }
        }
        let limit = Limit::new(seconds);
        // The git config entries the environment holds stay.
        let count: usize = std::env::var("GIT_CONFIG_COUNT")
            .ok()
            .and_then(|c| c.parse().ok())
            .unwrap_or(0);
        let mut env: Vec<(String, String)> = GO_OFFLINE
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        env.push((
            "GIT_CONFIG_COUNT".into(),
            (count + MUTATION_GIT.len()).to_string(),
        ));
        for (i, (key, value)) in MUTATION_GIT.iter().enumerate() {
            env.push((format!("GIT_CONFIG_KEY_{}", count + i), key.to_string()));
            env.push((format!("GIT_CONFIG_VALUE_{}", count + i), value.to_string()));
        }
        let env: Vec<(&str, &str)> = env.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
        let options = [
            "--workers".to_string(),
            MUTATION_WORKERS.to_string(),
            "--timeout-coefficient".into(),
            MUTATION_TIMEOUT_COEFFICIENT.to_string(),
        ];
        let scratch = tempfile::tempdir()?;
        for (i, folder) in folders.iter().enumerate() {
            let place = if folder.is_empty() {
                "the repository's root".to_string()
            } else {
                folder.clone()
            };
            let late = format!(
                "gremlins ran past Whooper's {} limit, so Whooper has no survived or caught lines in {place}.",
                limit_text(seconds)
            );
            if limit.left().is_zero() {
                problems.push(late);
                continue;
            }
            let report = scratch.path().join(format!("{i}.json"));
            let mut shown = vec![
                "gremlins".to_string(),
                "unleash".into(),
                "--diff".into(),
                merge_base.clone(),
            ];
            shown.extend(options.iter().cloned());
            tools.push(shown_command(
                folder,
                &format!("GOPROXY=off GOTOOLCHAIN=local {}", shell_join(&shown)),
            ));
            let mut args = vec![
                "unleash".to_string(),
                "--diff".into(),
                merge_base.clone(),
                "--output".into(),
            ];
            args.push(report.display().to_string());
            args.extend(options.iter().cloned());
            let (code, out, err) = match limit.run(program, &args, &head.join(folder), &env) {
                Ok(r) => r,
                Err(TimeLimit) => {
                    problems.push(late);
                    continue;
                }
            };
            match fs::read_to_string(&report) {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    // gremlins writes no report when it finds nothing to change.
                    if code != 0 {
                        let error = err
                            .split('\n')
                            .rev()
                            .find_map(|l| l.strip_prefix("ERROR: ").map(str::to_string))
                            .or_else(|| {
                                format!("{out}\n{err}")
                                    .split('\n')
                                    .rev()
                                    .find(|l| !l.trim().is_empty())
                                    .map(|l| l.trim().to_string())
                            })
                            .unwrap_or_else(|| format!("it exited with code {code}"));
                        let failed = out
                            .split('\n')
                            .find(|l| l.starts_with("--- FAIL: "))
                            .map(|l| format!(" ({})", l.trim()))
                            .unwrap_or_default();
                        problems.push(format!(
                            "gremlins stopped in {place} before it changed a line: {error}{failed}."
                        ));
                    }
                }
                Err(e) => problems.push(format!(
                    "Whooper could not read gremlins's report for {place}: {e}."
                )),
                Ok(text) => match serde_json::from_str::<Value>(&text) {
                    Ok(found) if found["files"].is_array() => {
                        for file in found["files"].as_array().unwrap() {
                            let name = file["file_name"].as_str().unwrap_or("");
                            let path = normalize(&if folder.is_empty() {
                                name.to_string()
                            } else {
                                format!("{folder}/{name}")
                            });
                            for mutant in file["mutations"].as_array().into_iter().flatten() {
                                results.push((path.clone(), mutant.clone()));
                            }
                        }
                    }
                    Ok(_) => problems.push(format!(
                        "Whooper could not read gremlins's report for {place}: 'files'."
                    )),
                    Err(e) => problems.push(format!(
                        "Whooper could not read gremlins's report for {place}: {e}."
                    )),
                },
            }
        }
    }
    let added: HashMap<String, std::collections::BTreeSet<i64>> = diff_files(&patch)
        .into_iter()
        .map(|f| (f.path, f.added))
        .collect();
    let not_run: HashSet<(String, i64)> = gaps
        .get("lines")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|l| Some((l["path"].as_str()?.to_string(), l["line"].as_i64()?)))
        .collect();
    let changed_paths: HashSet<&String> = changed.iter().map(|(p, _)| p).collect();
    let mut texts: HashMap<String, String> = HashMap::new();
    type Best<'s> = ((bool, i64), &'s str, String);
    let mut best: Vec<((String, i64), Best)> = vec![];
    for (path, mutant) in &results {
        let line = mutant["line"].as_i64();
        let status = match mutant["status"].as_str() {
            Some("LIVED") => "survived",
            Some("KILLED") => "caught",
            _ => continue,
        };
        let Some(line) = line else { continue };
        if !changed_paths.contains(path)
            || !added.get(path).is_some_and(|a| a.contains(&line))
            || not_run.contains(&(path.clone(), line))
        {
            continue;
        }
        let column = mutant["column"].as_i64().unwrap_or(0);
        let rank = (status != "survived", column);
        let at = best.iter().position(|(k, _)| *k == (path.clone(), line));
        if at.is_none_or(|i| rank < best[i].1.0) {
            let text = texts
                .entry(path.clone())
                .or_insert_with(|| committed(&head, &commit, path).unwrap_or_default());
            let change = mutation_change(text, line, column, mutant["type"].as_str().unwrap_or(""));
            let value = (rank, status, change);
            match at {
                Some(i) => best[i].1 = value,
                None => best.push(((path.clone(), line), value)),
            }
        }
    }
    type Entry<'s> = (bool, i64, &'s str, String);
    let mut by_file: Vec<(String, Vec<Entry>)> = vec![];
    for ((path, line), (rank, status, change)) in best {
        let entry = (rank.0, line, status, change);
        match by_file.iter_mut().find(|(p, _)| *p == path) {
            Some((_, list)) => list.push(entry),
            None => by_file.push((path, vec![entry])),
        }
    }
    let (mut lines, mut limits): (Vec<Value>, Vec<String>) = (vec![], vec![]);
    for (path, mut entries) in by_file {
        entries.sort();
        for (_, line, status, change) in entries.iter().take(MUTATION_LINES_PER_FILE) {
            lines.push(json!({"path": path, "line": line, "status": status, "change": change}));
        }
        if entries.len() > MUTATION_LINES_PER_FILE {
            limits.push(format!(
                "Whooper shows at most {MUTATION_LINES_PER_FILE} survived or caught lines a file, survived first, so it left out {} of {path}.",
                plural(entries.len() - MUTATION_LINES_PER_FILE, "line", "lines")
            ));
        }
    }
    if let Some(missing) = missing {
        limits.push(format!("{missing}, so Whooper did not change the changed Go lines on purpose to see whether a test catches the change (mutation testing)."));
    } else if problems.len() < folders.len() {
        limits.insert(0, "Whooper changed each changed Go line that tests ran with gremlins, one small change at a time, such as `>` to `>=`, and ran the tests of the line's package again: survived when every test still passed, caught when one failed. gremlins changes only arithmetic, comparisons, increments, and negative signs, so other lines, and lines only another package's tests run, show neither.".into());
    }
    if !node_changed.is_empty() {
        limits.push("Whooper changes only Go lines on purpose, so JavaScript and TypeScript lines show neither survived nor caught.".into());
    }
    let order = order_of(&patch);
    let survived = lines.iter().filter(|l| l["status"] == "survived").count();
    let caught = lines.iter().filter(|l| l["status"] == "caught").count();
    let mut all_lines: Vec<Value> = gaps
        .get("lines")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    all_lines.extend(lines);
    all_lines.sort_by_key(|l| {
        (
            l["path"]
                .as_str()
                .and_then(|p| order.get(p).copied())
                .unwrap_or(order.len()),
            l["line"].as_i64().unwrap_or(0),
        )
    });
    gaps.insert("lines".into(), Value::Array(all_lines));
    let tool = match gaps
        .get("tool")
        .and_then(Value::as_str)
        .filter(|t| !t.is_empty())
    {
        Some(t) => std::iter::once(t.to_string())
            .chain(tools)
            .collect::<Vec<_>>()
            .join("; "),
        None => tools.join("; "),
    };
    gaps.insert("tool".into(), json!(tool));
    let mut summary = gaps
        .get("summary")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    summary.insert("survived".into(), json!(survived));
    summary.insert("caught".into(), json!(caught));
    gaps.insert("summary".into(), Value::Object(summary));
    let mut all_limits: Vec<Value> = gaps
        .get("limits")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    all_limits.extend(limits.into_iter().chain(problems).map(Value::String));
    gaps.insert("limits".into(), Value::Array(all_limits));
    fsx::write_file(
        &run.join("test_gaps.json"),
        &fsx::pretty(&Value::Object(gaps)),
        false,
    )?;
    Ok(())
}

/// A relative path without . and .. parts, as posixpath.normpath writes it.
pub fn normalize(path: &str) -> String {
    let mut parts: Vec<&str> = vec![];
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if parts.last().is_some_and(|p| *p != "..") {
                    parts.pop();
                } else {
                    parts.push("..");
                }
            }
            p => parts.push(p),
        }
    }
    if parts.is_empty() {
        ".".into()
    } else {
        parts.join("/")
    }
}
