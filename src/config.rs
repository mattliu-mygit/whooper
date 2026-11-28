//! The user's config file, ~/.config/whooper/config.toml: where it is, what it holds, the checks every
//! command runs on it, the schedule, and the settings a settings form reads and saves.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use jiff::civil::{Date, DateTime, Time};
use serde_json::{Map, Value, json};
use toml_edit::{DocumentMut, Item};

use crate::fail;
use crate::fsx::{self, Object, Result, joined};

/// The jobs Whooper runs, each on the agent the config's [jobs] names for it.
pub const JOBS: [&str; 2] = ["review", "poll"];
/// The agents with a launcher for every job, in src/agent.rs.
pub const AGENTS: [&str; 2] = ["claude", "devin"];
pub const DAYS: [&str; 7] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
/// whooper-poll's time limit when the config's [poll] leaves out timeout_minutes.
pub const POLL_TIMEOUT_MINUTES: i64 = 10;
/// whooper-ask's time limit when the config's [ask] leaves out timeout_minutes.
pub const ASK_TIMEOUT_MINUTES: i64 = 10;

/// Whooper's folder: the program's own folder's parent, which holds schemas/, prompts/, queries/,
/// skills/, examples/, and lib/.
pub fn root() -> PathBuf {
    let exe = env::current_exe()
        .ok()
        .and_then(|p| fs::canonicalize(p).ok())
        .unwrap_or_default();
    // bin/whooper, or target/<profile>/whooper while developing.
    let bin = exe.parent().unwrap_or(Path::new("."));
    let up = bin.parent().unwrap_or(Path::new("."));
    if up.file_name().is_some_and(|n| n == "target")
        || up
            .parent()
            .and_then(|p| p.file_name())
            .is_some_and(|n| n == "target")
    {
        let mut folder = up.to_path_buf();
        while folder.file_name().is_some_and(|n| n != "target") {
            folder.pop();
        }
        folder.pop();
        return folder;
    }
    up.to_path_buf()
}

/// $WHOOPER_CONFIG, else whooper/config.toml under $XDG_CONFIG_HOME (default ~/.config).
pub fn config_path() -> PathBuf {
    if let Some(path) = env::var_os("WHOOPER_CONFIG").filter(|v| !v.is_empty()) {
        return PathBuf::from(path);
    }
    base("XDG_CONFIG_HOME", ".config")
        .join("whooper")
        .join("config.toml")
}

fn base(variable: &str, default: &str) -> PathBuf {
    env::var_os(variable)
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env::var_os("HOME").unwrap_or_default()).join(default))
}

/// Whooper's folder under an XDG base folder, such as $XDG_STATE_HOME/whooper.
pub fn xdg_dir(variable: &str, default: &str) -> PathBuf {
    base(variable, default).join("whooper")
}

/// The state folder, $XDG_STATE_HOME/whooper (default ~/.local/state/whooper).
pub fn state_dir() -> PathBuf {
    xdg_dir("XDG_STATE_HOME", ".local/state")
}

/// The partial clone of repo that reviews check out from: whooper/repos/<owner>-<name> under
/// $XDG_CACHE_HOME (default ~/.cache).
pub fn repo_clone(repo: &str) -> PathBuf {
    xdg_dir("XDG_CACHE_HOME", ".cache")
        .join("repos")
        .join(repo.replace('/', "-"))
}

/// The config, read as JSON values, with the folder it is in.
pub struct Config {
    pub values: Object,
    pub dir: PathBuf,
}

/// A TOML document's values as JSON, keeping integers and floats apart.
pub fn to_json(item: &Item) -> Value {
    match item {
        Item::None => Value::Null,
        Item::Value(value) => value_json(value),
        Item::Table(table) => Value::Object(
            table
                .iter()
                .map(|(k, v)| (k.to_string(), to_json(v)))
                .collect(),
        ),
        Item::ArrayOfTables(tables) => Value::Array(
            tables
                .iter()
                .map(|t| {
                    Value::Object(t.iter().map(|(k, v)| (k.to_string(), to_json(v))).collect())
                })
                .collect(),
        ),
    }
}

fn value_json(value: &toml_edit::Value) -> Value {
    use toml_edit::Value as V;
    match value {
        V::String(s) => json!(s.value()),
        V::Integer(i) => json!(*i.value()),
        V::Float(f) => json!(*f.value()),
        V::Boolean(b) => json!(*b.value()),
        V::Datetime(d) => json!(d.value().to_string()),
        V::Array(a) => Value::Array(a.iter().map(value_json).collect()),
        V::InlineTable(t) => Value::Object(
            t.iter()
                .map(|(k, v)| (k.to_string(), value_json(v)))
                .collect(),
        ),
    }
}

fn parse(text: &str, path: &Path) -> Result<Object> {
    let document: DocumentMut = text
        .parse()
        .map_err(|e| fsx::Exit::new(1, format!("whooper: {} is not TOML: {e}", path.display())))?;
    match to_json(document.as_item()) {
        Value::Object(map) => Ok(map),
        _ => Ok(Map::new()),
    }
}

/// Reads the config, without checking it.
pub fn load() -> Result<Config> {
    let path = config_path();
    let Some(text) = fs::read_to_string(&path).ok() else {
        fail!(
            "whooper: no config at {}; copy examples/config.example.toml there and fill it in",
            path.display()
        )
    };
    let values = parse(&text, &path)?;
    Ok(Config {
        values,
        dir: path.parent().map(Path::to_path_buf).unwrap_or_default(),
    })
}

/// Reads the config and runs the checks every command runs as it starts (check).
pub fn load_checked() -> Result<(Config, Limits)> {
    let config = load()?;
    let limits = check(&config.values)?;
    Ok((config, limits))
}

/// A table of the config, or an empty one.
pub fn table<'a>(values: &'a Object, key: &str) -> &'a Object {
    static EMPTY: std::sync::OnceLock<Object> = std::sync::OnceLock::new();
    values
        .get(key)
        .and_then(Value::as_object)
        .unwrap_or_else(|| EMPTY.get_or_init(Map::new))
}

/// A string the config must hold, such as [user] github_login.
pub fn required<'a>(values: &'a Object, section: &str, key: &str) -> Result<&'a str> {
    match table(values, section).get(key).and_then(Value::as_str) {
        Some(value) => Ok(value),
        None => fail!(
            "whooper: the config's [{section}] has no {key}; see {}",
            config_path().display()
        ),
    }
}

/// The time limits of the commands that run agents, in minutes.
pub struct Limits {
    pub poll: i64,
    pub review: i64,
    pub ask: f64,
}

/// An integer, not a float or a bool, as the whole-number settings take.
fn whole(value: Option<&Value>) -> Option<i64> {
    value.filter(|v| v.is_i64()).and_then(Value::as_i64)
}

/// Fails unless config's [jobs] names an agent with a launcher for each of JOBS, and each of [poll],
/// [review], and [ask] timeout_minutes is a time limit its command can use. Other keys in [jobs], such
/// as the summary job Whooper no longer runs, are ignored. A whole-number setting typed as a float,
/// such as 45.0, is refused: the page sends whole numbers, so only a hand edit writes one.
pub fn check(values: &Object) -> Result<Limits> {
    let jobs = table(values, "jobs");
    let missing: Vec<&str> = JOBS
        .iter()
        .copied()
        .filter(|job| !jobs.contains_key(*job))
        .collect();
    if !missing.is_empty() {
        fail!(
            "whooper: [jobs] names no agent for {}; each of review and poll names the agent it runs on; see {}",
            joined(&missing, "or"),
            config_path().display()
        );
    }
    for job in JOBS {
        let agent = &jobs[job];
        if !agent.as_str().is_some_and(|a| AGENTS.contains(&a)) {
            let shown = match agent {
                Value::String(s) => format!("'{s}'"),
                other => other.to_string(),
            };
            let name = agent.as_str().unwrap_or("?");
            fail!("whooper: [jobs] {job} names {shown}, which has no agents/{name}/{job}");
        }
    }
    let poll = match table(values, "poll").get("timeout_minutes") {
        None => Some(POLL_TIMEOUT_MINUTES),
        given => whole(given),
    };
    let Some(poll) = poll.filter(|&n| n >= 5) else {
        fail!(
            "whooper: [poll] timeout_minutes takes a whole number of minutes, at least 5; see {}",
            config_path().display()
        )
    };
    let Some(review) = whole(table(values, "review").get("timeout_minutes")).filter(|&n| n >= 1)
    else {
        fail!(
            "whooper: [review] timeout_minutes takes a whole number of minutes, at least 1; see {}",
            config_path().display()
        )
    };
    let ask = match table(values, "ask").get("timeout_minutes") {
        None => Some(ASK_TIMEOUT_MINUTES as f64),
        Some(v) if v.is_number() => v.as_f64(),
        Some(_) => None,
    };
    let Some(ask) = ask.filter(|&n| n > 0.0) else {
        fail!(
            "whooper: [ask] timeout_minutes takes a number of minutes above 0; see {}",
            config_path().display()
        )
    };
    Ok(Limits { poll, review, ask })
}

/// The config's [schedule], with Whooper's defaults for what it leaves out.
pub struct Schedule {
    pub interval_minutes: i64,
    pub start: Time,
    pub end: Time,
    pub days: Vec<String>,
}

fn schedule_defaults() -> Object {
    json!({"interval_minutes": 15, "start": "10:00", "end": "18:00", "days": ["mon", "tue", "wed", "thu", "fri"]})
        .as_object()
        .cloned()
        .unwrap()
}

fn clock_time(value: &Value) -> Option<Time> {
    let text = value.as_str()?;
    let (h, m) = text.split_once(':')?;
    if h.is_empty()
        || h.len() > 2
        || m.len() != 2
        || !h.chars().chain(m.chars()).all(|c| c.is_ascii_digit())
    {
        return None;
    }
    Time::new(h.parse().ok()?, m.parse().ok()?, 0, 0).ok()
}

/// Reads the schedule, and fails on one next_round cannot follow.
pub fn read_schedule(values: &Object) -> Result<Schedule> {
    let mut merged = schedule_defaults();
    for (k, v) in table(values, "schedule") {
        merged.insert(k.clone(), v.clone());
    }
    let path = config_path();
    let (Some(start), Some(end)) = (clock_time(&merged["start"]), clock_time(&merged["end"]))
    else {
        fail!(
            "whooper: [schedule] start and end take a 24-hour time such as \"18:00\"; see {}",
            path.display()
        )
    };
    let days: Vec<String> = merged["days"]
        .as_array()
        .map(|a| {
            a.iter()
                .map(|d| d.as_str().unwrap_or("").to_string())
                .collect()
        })
        .unwrap_or_default();
    let unknown: Vec<String> = days
        .iter()
        .filter(|d| !DAYS.contains(&d.as_str()))
        .map(|d| format!("'{d}'"))
        .collect();
    if !unknown.is_empty() {
        fail!(
            "whooper: [schedule] days takes names from {}, not [{}]",
            DAYS.join(", "),
            unknown.join(", ")
        );
    }
    let Some(interval) = whole(merged.get("interval_minutes")).filter(|&n| n >= 1) else {
        fail!(
            "whooper: [schedule] interval_minutes takes a whole number of minutes, at least 1; see {}",
            path.display()
        )
    };
    if days.is_empty() || start >= end {
        fail!(
            "whooper: [schedule] needs at least one day and an end after its start; see {}",
            path.display()
        );
    }
    Ok(Schedule {
        interval_minutes: interval,
        start,
        end,
        days,
    })
}

/// The first local time at or after earliest when whooper-run may start a round: on one of the
/// schedule's days, at or after its start and before its end. A round that starts before the end may
/// run past it. Times carry no zone, so after a daylight saving change rounds still start at the
/// configured hour.
pub fn next_round(schedule: &Schedule, earliest: DateTime) -> DateTime {
    for offset in 0..8 {
        let day: Date = earliest
            .date()
            .checked_add(jiff::Span::new().days(offset))
            .expect("dates in range");
        let name = DAYS[day.weekday().to_monday_zero_offset() as usize];
        if schedule.days.iter().any(|d| d == name) && (offset > 0 || earliest.time() < schedule.end)
        {
            return earliest.max(day.to_datetime(schedule.start));
        }
    }
    unreachable!("a schedule names at least one day")
}

/// The settings a settings form may edit, as a JSON Schema.
pub fn settings_schema() -> Result<Value> {
    let path = root().join("schemas").join("settings.json");
    Ok(serde_json::from_str(&fs::read_to_string(&path)?)?)
}

/// The config's values for the settings in the schema, with Whooper's defaults for what it leaves
/// out. A setting with neither is left out.
pub fn read_settings(values: &Object) -> Result<Value> {
    let mut all = values.clone();
    let mut with = |key: &str, defaults: Object| {
        let mut merged = defaults;
        for (k, v) in table(values, key) {
            merged.insert(k.clone(), v.clone());
        }
        all.insert(key.into(), Value::Object(merged));
    };
    with("schedule", schedule_defaults());
    with(
        "poll",
        json!({"timeout_minutes": POLL_TIMEOUT_MINUTES})
            .as_object()
            .cloned()
            .unwrap(),
    );
    with(
        "ask",
        json!({"timeout_minutes": ASK_TIMEOUT_MINUTES})
            .as_object()
            .cloned()
            .unwrap(),
    );
    fn pick(value: &Value, schema: &Value) -> Value {
        let Some(properties) = schema.get("properties").and_then(Value::as_object) else {
            return value.clone();
        };
        let mut out = Map::new();
        for (key, sub) in properties {
            if let Some(v) = value.get(key) {
                out.insert(key.clone(), pick(v, sub));
            }
        }
        Value::Object(out)
    }
    Ok(pick(&Value::Object(all), &settings_schema()?))
}

/// Saves values, part of the settings in the schema such as {"schedule": {"interval_minutes": 30}},
/// in the config file. Only the lines of the keys whose value or type changes are rewritten, keeping
/// comments, and a table the file lacks is added. Nothing is written unless the values fit the schema
/// and the config as it would be saved passes the checks Whooper runs when it starts. Prompting
/// Time's settings form and a terminal can each save at the same moment, so each save reads and
/// rewrites the file under its folder's lock. A config that links into a dotfiles folder stays a link.
pub fn write_settings(values: &Value) -> Result<()> {
    let schema = settings_schema()?;
    if let Err(e) = crate::schema::check(values, &schema, "settings") {
        fail!("whooper-settings: {e}");
    }
    let path = fs::canonicalize(config_path()).unwrap_or_else(|_| config_path());
    if !path.exists() {
        fail!("whooper-settings: no config at {}", path.display());
    }
    let _lock = fsx::folder_lock(path.parent().unwrap_or(Path::new(".")))?;
    let mut document: DocumentMut = fs::read_to_string(&path)?.parse().map_err(|e| {
        fsx::Exit::new(
            1,
            format!("whooper-settings: {} is not TOML: {e}", path.display()),
        )
    })?;
    merge(
        document.as_table_mut(),
        values.as_object().expect("the schema takes an object"),
    );
    let text = document.to_string();
    let merged = parse(&text, &path)?;
    read_schedule(&merged)?;
    check(&merged)?;
    fsx::write_file(&path, &text, false)?;
    Ok(())
}

fn merge(table: &mut toml_edit::Table, changes: &Object) {
    for (key, value) in changes {
        if let Value::Object(sub) = value {
            if !table.contains_key(key) {
                table.insert(key, Item::Table(toml_edit::Table::new()));
            }
            if let Some(Item::Table(inner)) = table.get_mut(key) {
                merge(inner, sub);
            }
            continue;
        }
        // Setting a key rewrites its line, which drops comments inside a list, so a value that did not
        // change keeps its line. A value of another type changed too, so a save of 15 over 15.0 writes 15.
        let current = table.get(key).map(to_json);
        if current.as_ref() == Some(value) && current.as_ref().map(kind) == Some(kind(value)) {
            continue;
        }
        let new = toml_value(value);
        match table.get_mut(key).and_then(Item::as_value_mut) {
            Some(old) => {
                let decor = old.decor().clone();
                *old = new;
                *old.decor_mut() = decor;
            }
            None => {
                table.insert(key, Item::Value(new));
            }
        }
    }
}

fn kind(value: &Value) -> u8 {
    match value {
        Value::Number(n) if n.is_i64() || n.is_u64() => 1,
        Value::Number(_) => 2,
        Value::Bool(_) => 3,
        Value::String(_) => 4,
        Value::Array(_) => 5,
        Value::Object(_) => 6,
        Value::Null => 0,
    }
}

fn toml_value(value: &Value) -> toml_edit::Value {
    match value {
        Value::String(s) => s.as_str().into(),
        Value::Bool(b) => (*b).into(),
        Value::Number(n) if n.is_i64() => n.as_i64().unwrap().into(),
        Value::Number(n) => n.as_f64().unwrap_or_default().into(),
        Value::Array(items) => toml_edit::Value::Array(items.iter().map(toml_value).collect()),
        Value::Object(map) => {
            let mut t = toml_edit::InlineTable::new();
            for (k, v) in map {
                t.insert(k, toml_value(v));
            }
            toml_edit::Value::InlineTable(t)
        }
        Value::Null => "".into(),
    }
}
