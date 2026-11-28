//! The connections step, connections.json: how the code a review's diff changes connects. The nodes are
//! the declarations the diff adds, changes, or removes, each found in the head, or a removed one at the
//! merge base, and the code that calls or uses them, one level up, found on the same side. Go files ask
//! gopls, on PATH or where go install puts it, and JavaScript and TypeScript files the TypeScript their
//! package installed, through lib/ts-callers.mjs, within TYPESCRIPT_SHARE of the step's limit. Every
//! other file, and a file whose tool is missing or cannot answer, gets name search: the definitions
//! LANGUAGES' patterns find, and git grep's whole-word matches of a name. A changed line counts toward
//! the innermost declaration around it. Each link says which found it, and limits says in plain words
//! what the graph can miss. The graph keeps at most MAX_NODES nodes: the changed code in the diff's
//! order, then the callers of each changed declaration in turn. Looking stops after SECONDS, and the
//! file then says so and holds what was found.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{Receiver, RecvTimeoutError, channel};
use std::time::{Duration, Instant};

use regex::Regex;
use serde_json::{Value, json};

use crate::config;
use crate::diff::{diff_files, json_lines};
use crate::fsx::{self, Result, limit_text, listed, plural, read_object};
use crate::proc;

pub const SECONDS: f64 = 120.0;
const MAX_NODES: usize = 40;
/// TypeScript's runs end once this share of the step's limit has passed, so name search keeps the rest
/// for the files TypeScript did not answer.
const TYPESCRIPT_SHARE: f64 = 0.75;

/// A declaration: its name, kind, the line of its name, and the lines it spans, from the comments above
/// it to its end; gopls also gives the position of its name.
#[derive(Clone, Debug)]
pub struct Symbol {
    pub name: String,
    pub kind: String,
    pub line: i64,
    pub start: i64,
    pub end: i64,
    indent: usize,
    position: Option<Value>,
}

impl Symbol {
    fn from_json(v: &Value) -> Option<Symbol> {
        Some(Symbol {
            name: v["name"].as_str()?.to_string(),
            kind: v["kind"].as_str()?.to_string(),
            line: v["line"].as_i64()?,
            start: v["start"].as_i64()?,
            end: v["end"].as_i64()?,
            indent: 0,
            position: None,
        })
    }
}

struct Language {
    name: &'static str,
    extensions: &'static [&'static str],
    comment: &'static str,
    above: &'static [&'static str],
    /// Each pattern, anchored at the line's start, with the kind of node it names; a pattern whose
    /// match ends at = must not be followed by another =.
    definitions: Vec<(Regex, &'static str, bool)>,
}

fn languages() -> &'static [Language] {
    static ALL: std::sync::OnceLock<Vec<Language>> = std::sync::OnceLock::new();
    ALL.get_or_init(|| {
        let r = |p: &str| Regex::new(&format!("^(?:{p})")).unwrap();
        vec![
            Language {
                name: "go",
                extensions: &[".go"],
                comment: "//",
                above: &["//"],
                definitions: vec![
                    (r(r"func\s+\((?:\w+\s+)?(?P<recv>\*?\w+(?:\[[^\]]*\])?)\)\s*(?P<name>\w+)"), "method", false),
                    (r(r"func\s+(?P<name>\w+)"), "func", false),
                    (r(r"type\s+(?P<name>\w+)"), "type", false),
                    (r(r"var\s+(?P<name>\w+)"), "var", false),
                    (r(r"const\s+(?P<name>\w+)"), "const", false),
                ],
            },
            Language {
                name: "python",
                extensions: &[".py", ".pyi"],
                comment: "#",
                above: &["#", "@"],
                definitions: vec![
                    (r(r"\s*(?:async\s+)?def\s+(?P<name>\w+)"), "func", false),
                    (r(r"\s*class\s+(?P<name>\w+)"), "type", false),
                    (r(r"(?P<name>[A-Z][A-Z0-9_]*)\s*(?::[^=]*)?="), "const", true),
                    (r(r"(?P<name>[A-Za-z_]\w*)\s*(?::[^=]*)?="), "var", true),
                ],
            },
            Language {
                name: "javascript",
                extensions: &[".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"],
                comment: "//",
                above: &["//", "/*", "*", "@"],
                definitions: vec![
                    (r(r"\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\b\s*\*?\s*(?P<name>[\w$]+)"), "func", false),
                    (r(r"\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>[\w$]+)"), "type", false),
                    (r(r"\s*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+(?P<name>[\w$]+)"), "type", false),
                    (r(r"\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[\w$]+)\s*(?::[^=]*)?=\s*(?:async\s+)?(?:function\b|\(|[\w$]+\s*=>)"), "func", false),
                    (r(r"(?:export\s+)?const\s+(?P<name>[\w$]+)"), "const", false),
                    (r(r"(?:export\s+)?(?:let|var)\s+(?P<name>[\w$]+)"), "var", false),
                ],
            },
        ]
    })
}

fn spec(language: &str) -> &'static Language {
    languages().iter().find(|l| l.name == language).unwrap()
}

/// The language whose extensions path has, or None.
pub fn language_of(path: &str) -> Option<&'static str> {
    languages()
        .iter()
        .find(|l| l.extensions.iter().any(|e| path.ends_with(e)))
        .map(|l| l.name)
}

const PYTHON_KEYWORDS: [&str; 35] = [
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import",
    "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while",
    "with", "yield",
];

/// The definitions name search finds in text, a file in language, by LANGUAGES' patterns, each spanning
/// its lines from the comments and decorators directly above it to the line before the next definition,
/// as git's hunk headers count each line toward the nearest definition above it. A Python def inside a
/// class is a method named with its class, as in Cart.total. Go's var (, const (, and type ( blocks name
/// a declaration on each line indented one tab.
pub fn definitions(text: &str, language: &str) -> Vec<Symbol> {
    let spec = spec(language);
    let lines: Vec<&str> = text.split('\n').collect();
    let block_start = Regex::new(r"^(var|const|type)\s*\($").unwrap();
    let block_line = Regex::new(r"^\t(\w+)").unwrap();
    let mut found: Vec<Symbol> = vec![];
    let mut block: Option<String> = None;
    let symbol = |name: String, kind: &str, line: usize, indent: usize| Symbol {
        name,
        kind: kind.into(),
        line: line as i64,
        start: line as i64,
        end: 0,
        indent,
        position: None,
    };
    for (i, line) in lines.iter().enumerate() {
        let number = i + 1;
        if let Some(kind) = &block {
            if line.starts_with(')') {
                block = None;
            } else if let Some(m) = block_line.captures(line) {
                found.push(symbol(m[1].to_string(), kind, number, 1));
            }
            continue;
        }
        if language == "go"
            && let Some(m) = block_start.captures(line)
        {
            block = Some(m[1].to_string());
            continue;
        }
        for (pattern, kind, no_double_equals) in &spec.definitions {
            let Some(m) = pattern.captures(line) else {
                continue;
            };
            if *no_double_equals && line[m.get(0).unwrap().end()..].starts_with('=') {
                continue;
            }
            let name = &m["name"];
            if language == "python" && PYTHON_KEYWORDS.contains(&name) {
                continue;
            }
            let name = match m.name("recv") {
                Some(recv) => format!("({}).{name}", recv.as_str()),
                None => name.to_string(),
            };
            let indent = line.chars().count() - line.trim_start().chars().count();
            found.push(symbol(name, kind, number, indent));
            break;
        }
    }
    for i in 0..found.len() {
        if language == "python" && found[i].kind == "func" && found[i].indent > 0 {
            let indent = found[i].indent;
            if let Some(outer) = found[..i].iter().rev().find(|s| s.indent < indent).cloned()
                && outer.kind == "type"
            {
                found[i].kind = "method".into();
                found[i].name = format!("{}.{}", outer.name, found[i].name);
            }
        }
        while found[i].start > 1
            && spec
                .above
                .iter()
                .any(|a| lines[found[i].start as usize - 2].trim().starts_with(a))
        {
            found[i].start -= 1;
        }
    }
    for i in 0..found.len() {
        found[i].end = match found.get(i + 1) {
            Some(after) => after.start - 1,
            None => lines.len() as i64,
        };
    }
    found
}

/// The text of path in commit, read in the worktree tree without fetching, or None when commit has no
/// such file.
pub fn committed(tree: &Path, commit: &str, path: &str) -> Option<String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(tree)
        .args([
            "--no-lazy-fetch",
            "cat-file",
            "blob",
            &format!("{commit}:{path}"),
        ])
        .stdin(Stdio::null())
        .output()
        .ok()?;
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

/// The lines of commit's files in language that spell out name as a whole word, each as (path, line),
/// from git grep, which reads the commit's files and fetches nothing. A line that starts with the
/// language's line comment is left out.
pub fn name_search(
    tree: &Path,
    commit: &str,
    name: &str,
    language: &str,
) -> std::result::Result<Vec<(String, i64)>, String> {
    let spec = spec(language);
    let out = Command::new("git")
        .arg("-C")
        .arg(tree)
        .args([
            "--no-lazy-fetch",
            "grep",
            "-z",
            "-n",
            "-w",
            "-F",
            "-I",
            "--no-color",
            "--no-column",
            "-e",
            name,
            commit,
            "--",
        ])
        .args(spec.extensions.iter().map(|e| format!("*{e}")))
        .stdin(Stdio::null())
        .output()
        .map_err(|e| e.to_string())?;
    if out.status.code().unwrap_or(2) > 1 {
        return Err(format!(
            "git grep for {name} failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    let prefix = format!("{commit}:");
    let mut sites = vec![];
    for record in String::from_utf8_lossy(&out.stdout).split('\n') {
        let mut parts = record.splitn(3, '\0');
        let (Some(at), Some(number), Some(text)) = (parts.next(), parts.next(), parts.next())
        else {
            continue;
        };
        if !text.trim_start().starts_with(spec.comment) {
            sites.push((
                at.strip_prefix(&prefix).unwrap_or(at).to_string(),
                number.parse().unwrap_or(0),
            ));
        }
    }
    Ok(sites)
}

/// The files with one of extensions that git status lists in the worktree tree: those a review run
/// changed, added, or deleted there, such as a probe's test.
pub fn edited_files(tree: &Path, extensions: &[&str]) -> std::result::Result<Vec<String>, String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(tree)
        .args([
            "--no-optional-locks",
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
        ])
        .stdin(Stdio::null())
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!(
            "git status failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    let text = String::from_utf8_lossy(&out.stdout).into_owned();
    let mut entries = text.split('\0');
    let mut paths = vec![];
    while let Some(entry) = entries.next() {
        // A rename or copy names the path it came from next.
        if entry.starts_with('R') || entry.starts_with('C') {
            entries.next();
        }
        if extensions.iter().any(|e| entry.ends_with(e)) && entry.len() > 3 {
            paths.push(entry[3..].to_string());
        }
    }
    Ok(paths)
}

/// The program name on PATH, or None.
pub fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|d| d.join(name))
        .find(|p| {
            use std::os::unix::fs::PermissionsExt;
            fs::metadata(p).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        })
}

/// A Go tool such as gopls or gremlins, on PATH or where go install puts it, GOBIN or the bin folder
/// of the first GOPATH folder, or why there is none.
pub fn find_go_tool(name: &str) -> (Option<PathBuf>, Option<String>) {
    if which("go").is_none() {
        return (
            None,
            Some(format!(
                "The go command, which {name} needs, is not on PATH"
            )),
        );
    }
    if let Some(found) = which(name) {
        return (Some(found), None);
    }
    let out = Command::new("go")
        .args(["env", "GOBIN", "GOPATH"])
        .stdin(Stdio::null())
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default();
    let mut lines = out.split('\n');
    let (gobin, gopath) = (lines.next().unwrap_or(""), lines.next().unwrap_or(""));
    let folder = if !gobin.is_empty() {
        Some(PathBuf::from(gobin))
    } else if !gopath.is_empty() {
        Some(PathBuf::from(gopath.split(':').next().unwrap_or("")).join("bin"))
    } else {
        None
    };
    if let Some(folder) = folder.filter(|f| f.is_absolute())
        && which_in(&folder, name)
    {
        return (Some(folder.join(name)), None);
    }
    (
        None,
        Some(format!(
            "Whooper found no {name} on PATH or where go install puts it, go env GOBIN or GOPATH/bin"
        )),
    )
}

fn which_in(folder: &Path, name: &str) -> bool {
    use std::os::unix::fs::PermissionsExt;
    fs::metadata(folder.join(name)).is_ok_and(|m| m.permissions().mode() & 0o111 != 0)
}

/// A path as a file URI, as Python's as_uri writes it.
fn uri_of(path: &Path) -> String {
    let mut out = String::from("file://");
    for b in path.to_string_lossy().bytes() {
        if b.is_ascii_alphanumeric() || b"/_.-~".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn path_of(uri: &str) -> PathBuf {
    let rest = uri.strip_prefix("file://").unwrap_or(uri);
    let bytes = rest.as_bytes();
    let mut out = vec![];
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && i + 2 < bytes.len()
            && let Ok(b) = u8::from_str_radix(&rest[i + 1..i + 3], 16)
        {
            out.push(b);
            i += 3;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    PathBuf::from(String::from_utf8_lossy(&out).into_owned())
}

/// Why looking stopped short.
#[derive(Debug)]
enum Stop {
    /// The step's limit passed.
    TimeLimit,
    /// The language server answered a request with an error.
    Answer(String),
    /// The language server is gone or wrote something other than the protocol.
    Gone(String),
}

/// gopls, Go's language server, run as gopls serve in the worktree and spoken to in the Language Server
/// Protocol, so one load of the worktree's code answers every question. It runs with GOPROXY=off, so it
/// downloads nothing. Each file asked about opens first with its content in the commit, and so do the Go
/// files the review run changed or added, so neither the run's edits nor its probes reach the answers.
/// It runs in a session of its own, with the go commands it starts, and close stops all of it.
struct Gopls {
    tree: PathBuf,
    commit: String,
    child: Child,
    input: ChildStdin,
    messages: Receiver<std::result::Result<Value, String>>,
    requests: i64,
    opened: HashSet<String>,
    errors: Vec<String>,
    deadline: Instant,
}

const GOPLS_KINDS: [(i64, &str); 7] = [
    (12, "func"),
    (6, "method"),
    (23, "type"),
    (11, "type"),
    (5, "type"),
    (13, "var"),
    (14, "const"),
];

impl Gopls {
    fn start(
        program: &Path,
        tree: &Path,
        commit: &str,
        deadline: Instant,
    ) -> std::result::Result<Gopls, Stop> {
        let mut command = Command::new(program);
        command
            .arg("serve")
            .current_dir(tree)
            .env("GOPROXY", "off")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut child = proc::spawn(&mut command).map_err(|e| Stop::Gone(e.to_string()))?;
        let input = child.stdin.take().unwrap();
        let output = child.stdout.take().unwrap();
        let (send, messages) = channel();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(output);
            loop {
                let mut length = None;
                loop {
                    let mut line = String::new();
                    match reader.read_line(&mut line) {
                        Ok(0) | Err(_) => {
                            let _ = send.send(Err("gone".into()));
                            return;
                        }
                        Ok(_) => {}
                    }
                    let line = line.trim();
                    if line.is_empty() {
                        break;
                    }
                    if let Some((name, value)) = line.split_once(':')
                        && name.eq_ignore_ascii_case("content-length")
                    {
                        length = value.trim().parse::<usize>().ok();
                    }
                }
                let Some(length) = length else {
                    let _ = send.send(Err("gone".into()));
                    return;
                };
                let mut body = vec![0; length];
                if reader.read_exact(&mut body).is_err() {
                    let _ = send.send(Err("gone".into()));
                    return;
                }
                let message = serde_json::from_slice(&body).map_err(|e| format!("not JSON: {e}"));
                if send.send(message).is_err() {
                    return;
                }
            }
        });
        let mut server = Gopls {
            tree: tree.to_path_buf(),
            commit: commit.to_string(),
            child,
            input,
            messages,
            requests: 0,
            opened: HashSet::new(),
            errors: vec![],
            deadline,
        };
        let started = (|| {
            let uri = uri_of(tree);
            server.call(
                "initialize",
                json!({"processId": std::process::id(), "rootUri": uri,
                       "workspaceFolders": [{"uri": uri, "name": fsx::name_of(tree)}],
                       "capabilities": {"textDocument": {"documentSymbol": {"hierarchicalDocumentSymbolSupport": true}}}}),
            )?;
            server.send(json!({"method": "initialized", "params": {}}))?;
            for path in edited_files(tree, &[".go"]).map_err(Stop::Gone)? {
                let text = committed(tree, commit, &path).unwrap_or_else(|| server.blank(&path));
                server.open(&path, &text)?;
            }
            Ok(())
        })();
        match started {
            Ok(()) => Ok(server),
            Err(e) => {
                server.close();
                Err(e)
            }
        }
    }

    /// What a Go file the review run added opens as: its package clause alone, so gopls knows which of
    /// the worktree's modules it belongs to.
    fn blank(&self, path: &str) -> String {
        let clause = Regex::new(r"(?m)^package\s+\w+").unwrap();
        fsx::read_text(&self.tree.join(path))
            .and_then(|t| clause.find(&t).map(|m| format!("{}\n", m.as_str())))
            .unwrap_or_default()
    }

    fn gone(&mut self) -> Stop {
        if Instant::now() >= self.deadline {
            return Stop::TimeLimit;
        }
        let status = self.child.wait().map(proc::code).unwrap_or(1);
        Stop::Gone(if status > 128 {
            format!("gopls was stopped by signal {}", status - 128)
        } else {
            format!("gopls exited with code {status}")
        })
    }

    fn send(&mut self, message: Value) -> std::result::Result<(), Stop> {
        let mut message = message;
        message["jsonrpc"] = json!("2.0");
        let body = message.to_string();
        let framed = format!("Content-Length: {}\r\n\r\n{body}", body.len());
        if self
            .input
            .write_all(framed.as_bytes())
            .and_then(|_| self.input.flush())
            .is_err()
        {
            return Err(self.gone());
        }
        Ok(())
    }

    /// The server's answer to method with params. Meanwhile its notifications are dropped, except that
    /// each error it shows the user is kept, and a request of its own gets null.
    fn call(&mut self, method: &str, params: Value) -> std::result::Result<Value, Stop> {
        self.requests += 1;
        let id = self.requests;
        self.send(json!({"id": id, "method": method, "params": params}))?;
        loop {
            let left = self.deadline.saturating_duration_since(Instant::now());
            let message = match self.messages.recv_timeout(left) {
                Ok(Ok(message)) => message,
                Ok(Err(e)) if e != "gone" => return Err(Stop::Gone(e)),
                Err(RecvTimeoutError::Timeout) => return Err(Stop::TimeLimit),
                _ => return Err(self.gone()),
            };
            if message["method"] == "window/showMessage" && message["params"]["type"] == 1 {
                self.errors.push(
                    message["params"]["message"]
                        .as_str()
                        .unwrap_or("")
                        .to_string(),
                );
            }
            if message.get("method").is_some() {
                if let Some(request) = message.get("id") {
                    self.send(json!({"id": request, "result": null}))?;
                }
            } else if message["id"] == id {
                if let Some(error) = message.get("error") {
                    return Err(Stop::Answer(
                        error["message"].as_str().unwrap_or("").to_string(),
                    ));
                }
                return Ok(message.get("result").cloned().unwrap_or(Value::Null));
            }
        }
    }

    fn uri(&self, path: &str) -> String {
        uri_of(&self.tree.join(path))
    }

    fn open(&mut self, path: &str, text: &str) -> std::result::Result<(), Stop> {
        if self.opened.insert(path.to_string()) {
            let uri = self.uri(path);
            self.send(json!({"method": "textDocument/didOpen", "params": {
                "textDocument": {"uri": uri, "languageId": "go", "version": 1, "text": text}}}))?;
        }
        Ok(())
    }

    /// The declarations of path in the commit, from gopls: each spans its declaration and the comment
    /// lines directly above it, and keeps the position of its name, which sites asks about.
    fn symbols(&mut self, path: &str) -> std::result::Result<Vec<Symbol>, Stop> {
        let Some(text) = committed(&self.tree, &self.commit, path) else {
            return Ok(vec![]);
        };
        self.open(path, &text)?;
        let lines: Vec<&str> = text.split('\n').collect();
        let uri = self.uri(path);
        let answer = self.call(
            "textDocument/documentSymbol",
            json!({"textDocument": {"uri": uri}}),
        )?;
        let mut found = vec![];
        for symbol in answer.as_array().into_iter().flatten() {
            let Some(kind) = GOPLS_KINDS
                .iter()
                .find(|(k, _)| symbol["kind"].as_i64() == Some(*k))
                .map(|(_, k)| *k)
            else {
                continue;
            };
            let mut start = symbol["range"]["start"]["line"].as_i64().unwrap_or(0) + 1;
            while start > 1 && lines[start as usize - 2].trim().starts_with("//") {
                start -= 1;
            }
            found.push(Symbol {
                name: symbol["name"].as_str().unwrap_or("").to_string(),
                kind: kind.into(),
                line: symbol["selectionRange"]["start"]["line"]
                    .as_i64()
                    .unwrap_or(0)
                    + 1,
                start,
                end: symbol["range"]["end"]["line"].as_i64().unwrap_or(0) + 1,
                indent: 0,
                position: Some(symbol["selectionRange"]["start"].clone()),
            });
        }
        Ok(found)
    }

    /// Where code calls symbol, as gopls's incoming calls list them for a func or method, or uses it,
    /// as its references list them for any other kind: each as (path, line) in the worktree, sorted.
    fn sites(
        &mut self,
        path: &str,
        symbol: &Symbol,
    ) -> std::result::Result<Vec<(String, i64)>, Stop> {
        let document = json!({"uri": self.uri(path)});
        let position = symbol.position.clone().unwrap_or(Value::Null);
        let mut places: Vec<(String, Value)> = vec![];
        if symbol.kind == "func" || symbol.kind == "method" {
            let items = self.call(
                "textDocument/prepareCallHierarchy",
                json!({"textDocument": document, "position": position}),
            )?;
            if let Some(item) = items.as_array().and_then(|a| a.first()).cloned() {
                let calls = self.call("callHierarchy/incomingCalls", json!({"item": item}))?;
                for call in calls.as_array().into_iter().flatten() {
                    for range in call["fromRanges"].as_array().into_iter().flatten() {
                        places.push((
                            call["from"]["uri"].as_str().unwrap_or("").to_string(),
                            range.clone(),
                        ));
                    }
                }
            }
        } else {
            let locations = self.call(
                "textDocument/references",
                json!({"textDocument": document, "position": position, "context": {"includeDeclaration": false}}),
            )?;
            for location in locations.as_array().into_iter().flatten() {
                places.push((
                    location["uri"].as_str().unwrap_or("").to_string(),
                    location["range"].clone(),
                ));
            }
        }
        let mut sites = vec![];
        for (uri, range) in places {
            let file = path_of(&uri);
            if let Ok(relative) = file.strip_prefix(&self.tree) {
                sites.push((
                    relative.to_string_lossy().into_owned(),
                    range["start"]["line"].as_i64().unwrap_or(0) + 1,
                ));
            }
        }
        sites.sort();
        Ok(sites)
    }

    /// Asks the server to shut down and exit, as the protocol has a client do, giving it a second, then
    /// stops what is left of it and of every process group below it.
    fn close(&mut self) {
        self.deadline = self.deadline.min(Instant::now() + Duration::from_secs(1));
        if self.call("shutdown", Value::Null).is_ok() {
            let _ = self.send(json!({"method": "exit"}));
            let end = Instant::now() + Duration::from_secs(1);
            while Instant::now() < end && !matches!(self.child.try_wait(), Ok(Some(_))) {
                std::thread::sleep(Duration::from_millis(20));
            }
        }
        proc::stop_tree(self.child.id() as i32);
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// TypeScript's own tools for the JavaScript and TypeScript files of a review run, in a checkout, the
/// head's or the merge base's, with each typescript package a request needs: the one the file's package
/// installed, node_modules/typescript in the folder of the nearest package.json above the file or a
/// folder above that, in the file's checkout or else the head's, since the setup script installs
/// nothing at the merge base. A typescript package with lib/typescript.js, as TypeScript 5 and 6 have,
/// answers through lib/ts-callers.mjs, run with node once for each request. TypeScript 7 has no such
/// JavaScript API, so its files get name search, and limits says why. fallback holds, for each reason,
/// the changed files whose declarations or callers TypeScript did not give.
struct TypeScript<'a> {
    sides: &'a BTreeMap<&'static str, (PathBuf, String)>,
    seconds: f64,
    stop: Instant,
    declarations: BTreeMap<&'static str, HashMap<String, Vec<Symbol>>>,
    sites: HashMap<String, Vec<(String, i64)>>,
    fallback: Vec<(String, Vec<String>)>,
    hidden: HashMap<&'static str, Value>,
    broken: HashMap<PathBuf, String>,
    node: bool,
}

fn checkout(side: &str) -> &'static str {
    if side == "head" {
        "the head's checkout"
    } else {
        "the merge base's checkout"
    }
}

fn dirname(path: &str) -> &str {
    path.rfind('/').map(|i| &path[..i]).unwrap_or("")
}

fn join(folder: &str, name: &str) -> String {
    if folder.is_empty() {
        name.to_string()
    } else {
        format!("{folder}/{name}")
    }
}

const TYPESCRIPT_FILES: [&str; 9] = [
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".json",
];

impl TypeScript<'_> {
    fn fall_back(&mut self, why: &str, paths: &[String]) {
        let at = match self.fallback.iter().position(|(w, _)| w == why) {
            Some(at) => at,
            None => {
                self.fallback.push((why.to_string(), vec![]));
                self.fallback.len() - 1
            }
        };
        for path in paths {
            if !self.fallback[at].1.contains(path) {
                self.fallback[at].1.push(path.clone());
            }
        }
    }

    fn module(&self, side: &str, path: &str) -> std::result::Result<PathBuf, String> {
        let tree = &self.sides[side].0;
        let mut folder = dirname(path).to_string();
        while !tree.join(&folder).join("package.json").is_file() {
            if folder.is_empty() {
                return Err("no package.json is above {them}".into());
            }
            folder = dirname(&folder).to_string();
        }
        let mut checkouts = vec![tree.clone()];
        if self.sides["head"].0 != *tree {
            checkouts.push(self.sides["head"].0.clone());
        }
        for checkout in checkouts {
            let mut above = Some(checkout.join(&folder));
            while let Some(here) = above {
                if !here.starts_with(&checkout) {
                    break;
                }
                if here.join("node_modules/typescript/package.json").is_file() {
                    return Ok(here.join("node_modules/typescript"));
                }
                above = here.parent().map(Path::to_path_buf);
            }
        }
        Err("Whooper found no node_modules/typescript in {their} package or a folder above it, which the repo's setup script would install".into())
    }

    fn config(&self, side: &str, path: &str) -> Option<String> {
        let tree = &self.sides[side].0;
        let mut folder = dirname(path).to_string();
        loop {
            for name in ["tsconfig.json", "jsconfig.json"] {
                if tree.join(&folder).join(name).is_file() {
                    return Some(join(&folder, name));
                }
            }
            if folder.is_empty() {
                return None;
            }
            folder = dirname(&folder).to_string();
        }
    }

    fn out_of_time(&self) -> String {
        format!(
            "TypeScript's {} share of the step's {} limit ran out",
            limit_text(self.seconds * TYPESCRIPT_SHARE),
            limit_text(self.seconds)
        )
    }

    /// What lib/ts-callers.mjs answers in the checkout of side with module, for files, whose
    /// declarations it lists, and projects, the declarations whose callers it finds, by the path of their
    /// project's config, with why the run ended before it answered all.
    fn run(
        &mut self,
        side: &'static str,
        module: &Path,
        files: &[String],
        projects: &Value,
    ) -> (Vec<Value>, Option<String>) {
        if let Some(why) = self.broken.get(module) {
            return (vec![], Some(why.clone()));
        }
        let left = self.stop.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return (vec![], Some(self.out_of_time()));
        }
        if !module.join("lib/typescript.js").is_file() {
            let why = "{their} TypeScript has no lib/typescript.js, the JavaScript API Whooper asks, as TypeScript 7 does not".to_string();
            self.broken.insert(module.to_path_buf(), why.clone());
            return (vec![], Some(why));
        }
        if !self.node {
            return (
                vec![],
                Some("the node command, which TypeScript needs, is not on PATH".into()),
            );
        }
        let (tree, commit) = self.sides[side].clone();
        if !self.hidden.contains_key(side) {
            let mut hidden = serde_json::Map::new();
            for path in edited_files(&tree, &TYPESCRIPT_FILES).unwrap_or_default() {
                let text = committed(&tree, &commit, &path);
                hidden.insert(path, json!(text));
            }
            self.hidden.insert(side, Value::Object(hidden));
        }
        let Ok(scratch) = tempfile::tempdir() else {
            return (
                vec![],
                Some("Whooper could not make a temporary folder".into()),
            );
        };
        let request = scratch.path().join("request.json");
        let body = json!({"typescript": module.display().to_string(), "root": tree.display().to_string(),
                          "hidden": self.hidden[side], "files": files, "projects": projects});
        if fs::write(&request, body.to_string()).is_err() {
            return (
                vec![],
                Some("Whooper could not write its request to TypeScript".into()),
            );
        }
        let mut command = Command::new("node");
        command
            .arg(config::root().join("lib/ts-callers.mjs"))
            .arg(&request)
            .current_dir(&tree);
        let captured = match proc::run_captured(&mut command, Some(left)) {
            Ok(c) => c,
            Err(e) => {
                return (
                    vec![],
                    Some(format!("TypeScript failed in {} ({e})", checkout(side))),
                );
            }
        };
        let answers = json_lines(&captured.out);
        match captured.code {
            None => (answers, Some(self.out_of_time())),
            Some(0) => (answers, None),
            Some(code) => {
                eprint!("{}", captured.err);
                let why = answers
                    .iter()
                    .find(|a| {
                        a.as_object()
                            .is_some_and(|o| o.len() == 1 && o.contains_key("error"))
                    })
                    .and_then(|a| a["error"].as_str().map(str::to_string))
                    .unwrap_or_else(|| {
                        if code > 128 {
                            format!("node was stopped by signal {}", code - 128)
                        } else {
                            format!("node exited with code {code}")
                        }
                    });
                let why = format!("TypeScript failed in {} ({why})", checkout(side));
                self.broken.insert(module.to_path_buf(), why.clone());
                (answers, Some(why))
            }
        }
    }

    /// Lists the declarations of paths, changed files on side, in a run for each typescript package. A
    /// file TypeScript does not list keeps the declarations patterns find, and fallback says why.
    fn list(&mut self, side: &'static str, paths: &[String]) {
        let mut packages: Vec<(PathBuf, Vec<String>)> = vec![];
        let mut seen = HashSet::new();
        for path in paths {
            if !seen.insert(path.clone()) {
                continue;
            }
            match self.module(side, path) {
                Ok(module) => match packages.iter_mut().find(|(m, _)| *m == module) {
                    Some((_, files)) => files.push(path.clone()),
                    None => packages.push((module, vec![path.clone()])),
                },
                Err(why) => self.fall_back(&why, std::slice::from_ref(path)),
            }
        }
        for (module, files) in packages {
            let (answers, why) = self.run(side, &module, &files, &json!({}));
            for answer in &answers {
                if let Some(declared) = answer["declarations"].as_array() {
                    self.declarations.get_mut(side).unwrap().insert(
                        answer["file"].as_str().unwrap_or("").to_string(),
                        declared.iter().filter_map(Symbol::from_json).collect(),
                    );
                }
            }
            if let Some(why) = why {
                let missing: Vec<String> = files
                    .iter()
                    .filter(|p| !self.declarations[side].contains_key(*p))
                    .cloned()
                    .collect();
                self.fall_back(&why, &missing);
            }
        }
    }

    /// Finds the callers of nodes, changed declarations of files TypeScript listed on side, in a run for
    /// each typescript package. A node TypeScript gives no callers for gets them from name search, and
    /// fallback says why.
    fn find(&mut self, side: &'static str, nodes: &[&Node]) {
        type Projects<'n> = Vec<(String, Vec<&'n Node>)>;
        let mut packages: Vec<(PathBuf, Projects)> = vec![];
        for node in nodes {
            let Some(config) = self.config(side, &node.path) else {
                self.fall_back(
                    "Whooper found no tsconfig.json or jsconfig.json above {them}",
                    std::slice::from_ref(&node.path),
                );
                continue;
            };
            let module = self.module(side, &node.path).unwrap_or_default();
            let at = match packages.iter().position(|(m, _)| *m == module) {
                Some(at) => at,
                None => {
                    packages.push((module, vec![]));
                    packages.len() - 1
                }
            };
            let projects = &mut packages[at].1;
            match projects.iter_mut().find(|(c, _)| *c == config) {
                Some((_, group)) => group.push(node),
                None => projects.push((config, vec![node])),
            }
        }
        for (module, projects) in packages {
            let mut request = serde_json::Map::new();
            for (config, group) in &projects {
                request.insert(
                    config.clone(),
                    Value::Array(
                        group
                            .iter()
                            .map(|n| json!({"id": n.id, "path": n.path, "name": n.name, "calls": n.kind == "func" || n.kind == "method"}))
                            .collect(),
                    ),
                );
            }
            let (answers, why) = self.run(side, &module, &[], &Value::Object(request));
            let mut failed: HashMap<String, String> = HashMap::new();
            for answer in &answers {
                if let Some(declared) = answer["declarations"].as_array() {
                    self.declarations.get_mut(side).unwrap().insert(
                        answer["file"].as_str().unwrap_or("").to_string(),
                        declared.iter().filter_map(Symbol::from_json).collect(),
                    );
                } else if let Some(sites) = answer["sites"].as_array() {
                    let mut sites: Vec<(String, i64)> = sites
                        .iter()
                        .map(|s| {
                            (
                                s[0].as_str().unwrap_or("").to_string(),
                                s[1].as_i64().unwrap_or(0),
                            )
                        })
                        .collect();
                    sites.sort();
                    self.sites
                        .insert(answer["id"].as_str().unwrap_or("").to_string(), sites);
                } else if let Some(id) = answer["id"].as_str() {
                    failed.insert(
                        id.to_string(),
                        format!(
                            "TypeScript could not answer in {} ({})",
                            checkout(side),
                            answer["error"].as_str().unwrap_or("")
                        ),
                    );
                } else if let Some(project) = answer["project"].as_str() {
                    for (config, group) in &projects {
                        if config == project {
                            for node in group {
                                failed.insert(
                                    node.id.clone(),
                                    format!(
                                        "TypeScript could not load {project} in {} ({})",
                                        checkout(side),
                                        answer["error"].as_str().unwrap_or("")
                                    ),
                                );
                            }
                        }
                    }
                }
            }
            for (_, group) in &projects {
                for node in group {
                    if !self.sites.contains_key(&node.id) {
                        let why = failed
                            .get(&node.id)
                            .cloned()
                            .or(why.clone())
                            .unwrap_or_default();
                        self.fall_back(&why, std::slice::from_ref(&node.path));
                    }
                }
            }
        }
    }
}

/// A changed declaration.
#[derive(Clone)]
struct Node {
    id: String,
    name: String,
    kind: String,
    path: String,
    line: i64,
    lines_changed: i64,
    side: &'static str,
    change: &'static str,
    symbol: Symbol,
    language: &'static str,
}

impl Node {
    fn shown(&self) -> Value {
        json!({"id": self.id, "name": self.name, "kind": self.kind, "path": self.path, "line": self.line,
               "change": self.change, "lines_changed": self.lines_changed})
    }
}

enum Session {
    Running(Gopls),
    Failed,
}

/// The state of one look at the run's code.
struct Look<'a> {
    sides: &'a BTreeMap<&'static str, (PathBuf, String)>,
    program: Option<PathBuf>,
    sessions: HashMap<&'static str, Session>,
    cache: HashMap<(&'static str, String), Vec<Symbol>>,
    problems: Vec<String>,
    reported: Vec<String>,
    used: HashSet<String>,
    deadline: Instant,
}

impl Look<'_> {
    fn session(&mut self, side: &'static str) -> Option<&mut Gopls> {
        if let Some(program) = self.program.clone()
            && !self.sessions.contains_key(side)
        {
            self.sessions.insert(side, Session::Failed);
            let (tree, commit) = &self.sides[side];
            match Gopls::start(&program, tree, commit, self.deadline) {
                Ok(server) => {
                    self.sessions.insert(side, Session::Running(server));
                }
                Err(Stop::TimeLimit) => {}
                Err(Stop::Answer(e)) | Err(Stop::Gone(e)) => self.problems.push(format!(
                    "gopls did not start in {}: {e}, so its Go links come from name search.",
                    checkout(side)
                )),
            }
        }
        match self.sessions.get_mut(side) {
            Some(Session::Running(server)) => Some(server),
            _ => None,
        }
    }

    fn gopls_failed(&mut self, side: &'static str, e: &str) {
        if let Some(Session::Running(mut server)) = self.sessions.insert(side, Session::Failed) {
            self.reported.append(&mut server.errors);
            server.close();
        }
        self.problems.push(format!(
            "gopls stopped answering in {}: {e}, so the rest of its Go links come from name search.",
            checkout(side)
        ));
    }

    /// The declarations of path on side: from TypeScript for a file it listed, from gopls for a Go file
    /// while it answers, and otherwise from LANGUAGES' patterns.
    fn symbols(
        &mut self,
        ts: &TypeScript,
        side: &'static str,
        path: &str,
    ) -> std::result::Result<Vec<Symbol>, Stop> {
        if let Some(found) = ts.declarations[side].get(path) {
            return Ok(found.clone());
        }
        let key = (side, path.to_string());
        if let Some(found) = self.cache.get(&key) {
            return Ok(found.clone());
        }
        let language = language_of(path).unwrap_or("go");
        if language == "go"
            && let Some(gopls) = self.session(side)
        {
            match gopls.symbols(path) {
                Ok(found) => {
                    self.cache.insert(key, found.clone());
                    return Ok(found);
                }
                Err(Stop::TimeLimit) => return Err(Stop::TimeLimit),
                Err(Stop::Answer(e)) => self.problems.push(format!(
                    "gopls could not list the declarations of {path}: {e}."
                )),
                Err(Stop::Gone(e)) => self.gopls_failed(side, &e),
            }
        }
        let (tree, commit) = &self.sides[side];
        let found = committed(tree, commit, path)
            .map(|t| definitions(&t, language))
            .unwrap_or_default();
        self.used.insert("patterns".into());
        self.used.insert(language.into());
        self.cache.insert(key, found.clone());
        Ok(found)
    }

    fn check(&self) -> std::result::Result<(), Stop> {
        if Instant::now() >= self.deadline {
            return Err(Stop::TimeLimit);
        }
        Ok(())
    }

    fn close(&mut self) {
        for (_, session) in self.sessions.drain() {
            if let Session::Running(mut server) = session {
                self.reported.append(&mut server.errors);
                server.close();
            }
        }
    }
}

fn innermost(symbols: &[Symbol], line: i64) -> Option<&Symbol> {
    symbols
        .iter()
        .rev()
        .find(|s| s.start <= line && line <= s.end)
}

/// whooper step connections: writes connections.json in run, while its worktrees exist, within
/// seconds.
pub fn connections_within(run: &Path, seconds: f64) -> Result<()> {
    let item = read_object(&run.join("run.json")).unwrap_or_default();
    let patch = fsx::read_text(&run.join("diff.patch")).unwrap_or_default();
    let root = fs::canonicalize(run)?;
    let sides: BTreeMap<&'static str, (PathBuf, String)> = BTreeMap::from([
        (
            "head",
            (
                root.join("head"),
                fsx::str_of(&item, "head_sha").unwrap_or("").to_string(),
            ),
        ),
        (
            "base",
            (
                root.join("base"),
                fsx::str_of(&item, "merge_base").unwrap_or("").to_string(),
            ),
        ),
    ]);
    let (program, no_gopls) = find_go_tool("gopls");
    let started = Instant::now();
    let deadline = started + Duration::from_secs_f64(seconds);
    let mut look = Look {
        sides: &sides,
        program,
        sessions: HashMap::new(),
        cache: HashMap::new(),
        problems: vec![],
        reported: vec![],
        used: HashSet::new(),
        deadline,
    };
    let mut ts = TypeScript {
        sides: &sides,
        seconds,
        stop: started + Duration::from_secs_f64(seconds * TYPESCRIPT_SHARE),
        declarations: BTreeMap::from([("head", HashMap::new()), ("base", HashMap::new())]),
        sites: HashMap::new(),
        fallback: vec![],
        hidden: HashMap::new(),
        broken: HashMap::new(),
        node: which("node").is_some(),
    };
    let mut changed: Vec<Node> = vec![];
    let mut known: HashMap<String, Value> = HashMap::new();
    let mut callers: Vec<(String, Vec<String>)> = vec![];
    let mut edges: Vec<(String, String, &str, String)> = vec![];
    let mut skipped: Vec<String> = vec![];
    let mut languages_seen: HashSet<&str> = HashSet::new();
    let files = diff_files(&patch);
    let mut body = || -> std::result::Result<(), Stop> {
        // TypeScript first lists the declarations of the changed JavaScript and TypeScript files at both
        // commits, so the loop below counts their changed lines toward the declarations TypeScript sees.
        let head_js: Vec<String> = files
            .iter()
            .filter(|f| language_of(&f.path) == Some("javascript"))
            .map(|f| f.path.clone())
            .collect();
        ts.list("head", &head_js);
        let base_js: Vec<String> = files
            .iter()
            .filter(|f| language_of(&f.path) == Some("javascript"))
            .filter_map(|f| f.old.clone())
            .collect();
        ts.list("base", &base_js);
        for f in &files {
            look.check()?;
            let Some(language) = language_of(&f.path) else {
                skipped.push(f.path.clone());
                continue;
            };
            languages_seen.insert(language);
            // A declaration's changed lines are those the diff adds to it in the head and removes from it
            // at the merge base. A name declared twice in a file is one node.
            struct Entry {
                lines: i64,
                head: Option<(String, Symbol)>,
                base: Option<(String, Symbol)>,
            }
            let mut found: Vec<(String, Entry)> = vec![];
            let sides_lines = [
                ("head", Some(f.path.clone()), &f.added),
                ("base", f.old.clone(), &f.removed),
            ];
            for (side, file, lines) in sides_lines {
                let declared = match &file {
                    Some(file) => look.symbols(&ts, side, file)?,
                    None => vec![],
                };
                for symbol in &declared {
                    let at = match found.iter().position(|(n, _)| *n == symbol.name) {
                        Some(at) => at,
                        None => {
                            found.push((
                                symbol.name.clone(),
                                Entry {
                                    lines: 0,
                                    head: None,
                                    base: None,
                                },
                            ));
                            found.len() - 1
                        }
                    };
                    let slot = if side == "head" {
                        &mut found[at].1.head
                    } else {
                        &mut found[at].1.base
                    };
                    if slot.is_none() {
                        *slot = Some((file.clone().unwrap(), symbol.clone()));
                    }
                }
                for &n in lines.iter() {
                    if let Some(symbol) = innermost(&declared, n)
                        && let Some((_, entry)) =
                            found.iter_mut().find(|(name, _)| *name == symbol.name)
                    {
                        entry.lines += 1;
                    }
                }
            }
            for (name, entry) in found {
                if entry.lines == 0 {
                    continue;
                }
                let both = entry.head.is_some() && entry.base.is_some();
                let (side, (file, symbol)) = match entry.head {
                    Some(h) => ("head", h),
                    None => ("base", entry.base.unwrap()),
                };
                let node = Node {
                    id: format!("{file}:{name}"),
                    name: name.clone(),
                    kind: symbol.kind.clone(),
                    path: file,
                    line: symbol.line,
                    lines_changed: entry.lines,
                    side,
                    change: if both {
                        "changed"
                    } else if side == "head" {
                        "added"
                    } else {
                        "removed"
                    },
                    symbol,
                    language,
                };
                known.insert(node.id.clone(), node.shown());
                changed.push(node);
            }
        }
        // TypeScript finds the callers of the changed declarations it listed, the head's first.
        let capped: Vec<Node> = changed.iter().take(MAX_NODES).cloned().collect();
        for side in ["head", "base"] {
            let nodes: Vec<&Node> = capped
                .iter()
                .filter(|n| n.side == side && ts.declarations[side].contains_key(&n.path))
                .collect();
            ts.find(side, &nodes);
        }
        for node in &capped {
            look.check()?;
            let side = node.side;
            let mut sites = None;
            let mut found_by = "";
            if node.language == "go"
                && node.symbol.position.is_some()
                && let Some(gopls) = look.session(side)
            {
                match gopls.sites(&node.path, &node.symbol) {
                    Ok(s) => {
                        sites = Some(s);
                        found_by = "gopls";
                    }
                    Err(Stop::TimeLimit) => return Err(Stop::TimeLimit),
                    Err(Stop::Answer(e)) => look.problems.push(format!(
                        "gopls could not answer for {}: {e}, so its callers come from name search.",
                        node.name
                    )),
                    Err(Stop::Gone(e)) => look.gopls_failed(side, &e),
                }
            } else if let Some(s) = ts.sites.get(&node.id) {
                sites = Some(s.clone());
                found_by = "typescript";
            }
            let sites = match sites {
                Some(s) => s,
                None => {
                    // A method by its own name, as Total for (*Cart).Total and total for Cart.total.
                    let short = node.name.rsplit(['.', ')']).next().unwrap_or(&node.name);
                    let (tree, commit) = &sides[side];
                    found_by = "name_search";
                    name_search(tree, commit, short, node.language).map_err(Stop::Gone)?
                }
            };
            look.used.insert(found_by.into());
            let kind = if node.kind == "func" || node.kind == "method" {
                "calls"
            } else {
                "uses"
            };
            callers.push((node.id.clone(), vec![]));
            for (path, line) in sites {
                let declared = look.symbols(&ts, side, &path)?;
                let Some(caller) = innermost(&declared, line).cloned() else {
                    continue;
                };
                let caller_id = format!("{path}:{}", caller.name);
                if caller_id == node.id {
                    continue;
                }
                let mine = &mut callers.last_mut().unwrap().1;
                if !known.contains_key(&caller_id) {
                    if mine.len() == MAX_NODES {
                        look.used.insert("more".into());
                        break;
                    }
                    // Code the diff leaves alone shows where it is in the head, though it is found calling
                    // removed code at the merge base.
                    let mut shown = caller.clone();
                    if side == "base" {
                        let head = look.symbols(&ts, "head", &path)?;
                        if let Some(h) = head.iter().find(|s| s.name == caller.name) {
                            shown = h.clone();
                        }
                    }
                    known.insert(
                        caller_id.clone(),
                        json!({"id": caller_id, "name": shown.name, "kind": shown.kind, "path": path,
                               "line": shown.line, "change": "unchanged", "lines_changed": 0}),
                    );
                }
                let mine = &mut callers.last_mut().unwrap().1;
                if !mine.contains(&caller_id) {
                    mine.push(caller_id.clone());
                }
                let edge = (caller_id, node.id.clone(), kind, found_by.to_string());
                if !edges.contains(&edge) {
                    edges.push(edge);
                }
            }
        }
        Ok(())
    };
    let outcome = body();
    look.close();
    let stopped = match outcome {
        Ok(()) => None,
        Err(Stop::TimeLimit) => Some(format!(
            "Whooper stopped looking at its {}-second limit, so changed code and callers may be missing.",
            fsx::number(seconds)
        )),
        Err(Stop::Answer(e)) | Err(Stop::Gone(e)) => crate::fail!("whooper: {e}"),
    };
    let mut shown: Vec<(String, Value)> = vec![];
    let add = |shown: &mut Vec<(String, Value)>, id: &str, node: Value| {
        if !shown.iter().any(|(i, _)| i == id) {
            shown.push((id.to_string(), node));
        }
    };
    for node in changed.iter().take(MAX_NODES) {
        add(&mut shown, &node.id, node.shown());
    }
    // Then each changed declaration's first caller, then each one's second, and so on.
    let longest = callers.iter().map(|(_, c)| c.len()).max().unwrap_or(0);
    'turns: for turn in 0..longest {
        for (_, list) in &callers {
            if let Some(id) = list.get(turn) {
                if shown.len() == MAX_NODES {
                    break 'turns;
                }
                add(&mut shown, id, known[id].clone());
            }
        }
    }
    let found = known.len();
    let used = &look.used;
    let mut limits = vec![
        "Callers are one level up only: the code that calls those callers is not shown."
            .to_string(),
    ];
    if let Some(no_gopls) = &no_gopls
        && languages_seen.contains("go")
    {
        limits.push(format!(
            "{no_gopls}, so the Go links come from name search."
        ));
    }
    limits.extend(look.problems.iter().cloned());
    let mut reported = vec![];
    for error in &look.reported {
        if !reported.contains(error) {
            reported.push(error.clone());
        }
    }
    limits.extend(reported.iter().map(|e| format!("gopls reported: {e}")));
    for (why, paths) in &ts.fallback {
        let one = paths.len() == 1;
        let why = why
            .replace("{them}", if one { "it" } else { "them" })
            .replace("{their}", if one { "its" } else { "their" });
        limits.push(format!(
            "The links of {} come from name search, since {why}: {}.",
            plural(paths.len(), "changed file", "changed files"),
            listed(paths, 5)
        ));
    }
    if used.contains("gopls") {
        limits.push("gopls leaves out dynamic calls, such as calls through an interface, a function value, or reflection.".into());
    }
    if used.contains("typescript") {
        limits.push("TypeScript leaves out dynamic calls, such as calls through a function value, a value typed any, or a module import() loads at run time, and links a function only to code that calls it, not to code that only names it, such as a list of handlers.".into());
    }
    if used.contains("name_search") {
        limits.push("Name search links every whole-word match of a name, so a link can join code that only shares the name, and it misses calls that do not spell the name out.".into());
    }
    if used.contains("patterns") {
        limits.push("Where neither gopls nor TypeScript read a file, Whooper finds its definitions by a pattern per language and counts each changed line toward the nearest definition above it, so a change between two definitions can count toward the wrong one.".into());
    }
    if used.contains("javascript") {
        limits.push("In the JavaScript and TypeScript files TypeScript did not read, a change to a method of a class counts toward the class.".into());
    }
    if changed.iter().any(|n| n.change == "removed") {
        limits.push("Removed code shows the callers it had at the merge base.".into());
    }
    if !skipped.is_empty() {
        let more = if skipped.len() > 5 {
            format!(", and {} more", skipped.len() - 5)
        } else {
            String::new()
        };
        limits.push(format!(
            "Whooper finds no definitions in files of other languages, so it leaves out {} changed {}: {}{more}.",
            skipped.len(),
            if skipped.len() == 1 { "file" } else { "files" },
            skipped.iter().take(5).cloned().collect::<Vec<_>>().join(", ")
        ));
    }
    if found > shown.len() {
        limits.push(format!(
            "Whooper shows {} of the {found}{} declarations it found, the changed code first, and leaves out the rest and their links.",
            shown.len(),
            if used.contains("more") { " or more" } else { "" }
        ));
    }
    limits.extend(stopped);
    let ids: HashSet<&String> = shown.iter().map(|(i, _)| i).collect();
    let edges: Vec<Value> = edges
        .iter()
        .filter(|(a, b, ..)| ids.contains(a) && ids.contains(b))
        .map(|(a, b, kind, by)| json!({"from": a, "to": b, "kind": kind, "found_by": by}))
        .collect();
    let nodes: Vec<Value> = shown.into_iter().map(|(_, n)| n).collect();
    fsx::write_file(
        &run.join("connections.json"),
        &fsx::pretty(&json!({"nodes": nodes, "edges": edges, "limits": limits})),
        false,
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn definitions_span_from_their_comments_to_the_next_and_name_methods() {
        let go = definitions(
            "package p\n\n// Total sums.\nfunc (c *Cart) Total() int {\n\treturn 0\n}\n\nvar (\n\tA = 1\n)\n",
            "go",
        );
        let names: Vec<(&str, &str, i64, i64)> = go
            .iter()
            .map(|s| (s.name.as_str(), s.kind.as_str(), s.start, s.end))
            .collect();
        assert_eq!(
            names,
            [("(*Cart).Total", "method", 3, 8), ("A", "var", 9, 11)]
        );
        let py = definitions(
            "class Cart:\n    def total(self):\n        x = 1\n\nLIMIT = 3\nif x == 2: pass\n",
            "python",
        );
        let names: Vec<(&str, &str)> = py
            .iter()
            .map(|s| (s.name.as_str(), s.kind.as_str()))
            .collect();
        assert_eq!(
            names,
            [
                ("Cart", "type"),
                ("Cart.total", "method"),
                ("LIMIT", "const")
            ]
        );
        let js = definitions("export const f = async (a) => a\nlet n = 1\n", "javascript");
        assert_eq!(
            js.iter().map(|s| s.kind.as_str()).collect::<Vec<_>>(),
            ["func", "var"]
        );
    }

    #[test]
    fn file_uris_round_trip() {
        let path = Path::new("/tmp/a b/c%d.go");
        assert_eq!(uri_of(path), "file:///tmp/a%20b/c%25d.go");
        assert_eq!(path_of(&uri_of(path)), path);
    }
}
