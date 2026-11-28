//! Files, JSON, and times as every command reads and writes them.

use std::fs;
use std::io::Write;
use std::os::fd::AsRawFd;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use jiff::{Timestamp, Zoned, tz::TimeZone};
use serde_json::{Map, Value};

/// A command's failure: the message it prints on its error output, and its exit code.
#[derive(Debug)]
pub struct Exit {
    pub code: i32,
    pub message: String,
}

pub type Result<T> = std::result::Result<T, Exit>;

impl Exit {
    pub fn new(code: i32, message: impl Into<String>) -> Self {
        Exit {
            code,
            message: message.into(),
        }
    }
}

impl<E: std::fmt::Display> From<E> for Exit {
    fn from(e: E) -> Self {
        Exit::new(1, format!("whooper: {e}"))
    }
}

/// Returns early with exit code 1 and the formatted message.
#[macro_export]
macro_rules! fail {
    ($($arg:tt)*) => { return Err($crate::fsx::Exit::new(1, format!($($arg)*))) };
}

pub type Object = Map<String, Value>;

/// Writes text to path through a temporary file beside it, which then replaces it, so a failure leaves
/// the old file whole and a program watching the folder, such as Prompting Time, never reads half a
/// file. A file that already exists keeps its mode. With exclusive, the write fails with
/// AlreadyExists when path exists, so of two writers only the first succeeds: unlike a rename, a link
/// fails when the name is taken.
pub fn write_file(path: &Path, text: &str, exclusive: bool) -> std::io::Result<()> {
    let folder = path.parent().unwrap_or(Path::new("."));
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mut temporary = tempfile::Builder::new()
        .prefix(&format!(".{name}."))
        .tempfile_in(folder)?;
    temporary.write_all(text.as_bytes())?;
    if exclusive {
        fs::hard_link(temporary.path(), path)?;
        return Ok(());
    }
    if let Ok(meta) = fs::metadata(path) {
        fs::set_permissions(
            temporary.path(),
            fs::Permissions::from_mode(meta.permissions().mode()),
        )?;
    } else {
        // tempfile makes the file 0600; a new file gets the usual mode, as Python's open gives it.
        let mask = unsafe { libc::umask(0o022) };
        unsafe { libc::umask(mask) };
        fs::set_permissions(
            temporary.path(),
            fs::Permissions::from_mode(0o666 & !(mask as u32)),
        )?;
    }
    temporary.persist(path).map_err(|e| e.error)?;
    Ok(())
}

/// Holds an exclusive flock on folder while it lives, for code that reads a file in folder and writes
/// it back: of two such writers at once, in any of Whooper's processes, the second waits for the first
/// and then reads what it wrote. The lock adds no file to the folder and ends with the value or the
/// process.
pub struct FolderLock(#[allow(dead_code)] fs::File);

pub fn folder_lock(folder: &Path) -> std::io::Result<FolderLock> {
    let file = fs::File::open(folder)?;
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(FolderLock(file))
}

/// The file's text, or None when it does not exist or cannot be read. Bytes that are not UTF-8
/// read as U+FFFD.
pub fn read_text(path: &Path) -> Option<String> {
    fs::read(path)
        .ok()
        .map(|bytes| String::from_utf8_lossy(&bytes).into_owned())
}

/// The JSON in the file at path, or None when the file is missing or holds no JSON.
pub fn read_json(path: &Path) -> Option<Value> {
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

/// The JSON object in the file at path, or None when the file is missing or holds no JSON object,
/// such as a run.json that whooper-review left empty or one cut short.
pub fn read_object(path: &Path) -> Option<Object> {
    match read_json(path)? {
        Value::Object(map) => Some(map),
        _ => None,
    }
}

/// JSON as Whooper writes its files and prints its answers: indented by two spaces.
pub fn pretty(value: &Value) -> String {
    serde_json::to_string_pretty(value).expect("JSON values always encode")
}

/// The string at key, or None when it is missing or not a string.
pub fn str_of<'a>(object: &'a Object, key: &str) -> Option<&'a str> {
    object.get(key).and_then(Value::as_str)
}

/// A Unix time as whooper-status and whooper-show print times: local, in ISO 8601 with the offset,
/// such as 2031-07-26T18:02:46-07:00, as Python's isoformat writes it, so old readers keep reading it.
pub fn local_time(unix: i64) -> String {
    iso(&Timestamp::from_second(unix)
        .expect("Unix times in range")
        .to_zoned(TimeZone::system()))
}

/// A zoned time in ISO 8601 with its offset and whole seconds.
pub fn iso(when: &Zoned) -> String {
    when.strftime("%Y-%m-%dT%H:%M:%S%:z").to_string()
}

/// The time now in the system's zone.
pub fn now() -> Zoned {
    Zoned::now()
}

/// The time now in UTC, as records write it, such as 2031-07-27T01:02:46+00:00.
pub fn now_utc() -> Zoned {
    Timestamp::now().to_zoned(TimeZone::UTC)
}

/// An ISO 8601 time with an offset or Z, as the files and GitHub write them; None for anything else.
pub fn parse_time(text: &str) -> Option<Timestamp> {
    text.parse::<Timestamp>().ok()
}

/// The modification time of path, as local_time prints it, or None when it cannot be read.
pub fn modified(path: &Path) -> Option<String> {
    let meta = fs::metadata(path).ok()?;
    let seconds = meta
        .modified()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_secs();
    Some(local_time(seconds as i64))
}

/// The entries of folder, sorted by name, or none when it cannot be read.
pub fn entries(folder: &Path) -> Vec<PathBuf> {
    let mut found: Vec<PathBuf> = fs::read_dir(folder)
        .map(|list| list.filter_map(|e| e.ok().map(|e| e.path())).collect())
        .unwrap_or_default();
    found.sort();
    found
}

/// The file name of path as text.
pub fn name_of(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// count with one or many, as in 1 test or 2 tests.
pub fn plural(count: usize, one: &str, many: &str) -> String {
    format!("{count} {}", if count == 1 { one } else { many })
}

/// paths for a sentence: the first most of them, and how many more.
pub fn listed(paths: &[String], most: usize) -> String {
    let shown = paths
        .iter()
        .take(most)
        .cloned()
        .collect::<Vec<_>>()
        .join(", ");
    if paths.len() > most {
        format!("{shown}, and {} more", paths.len() - most)
    } else {
        shown
    }
}

/// words for a sentence, joined with commas and conjunction, such as "poll", "review or poll", or
/// "review, poll, and ask".
pub fn joined(words: &[&str], conjunction: &str) -> String {
    match words {
        [] => String::new(),
        [one] => one.to_string(),
        [rest @ .., last] => {
            format!(
                "{}{} {conjunction} {last}",
                rest.join(", "),
                if rest.len() > 1 { "," } else { "" }
            )
        }
    }
}

/// A time limit in words, such as 10-minute or 8-second.
pub fn limit_text(seconds: f64) -> String {
    if seconds % 60.0 == 0.0 {
        format!("{}-minute", number(seconds / 60.0))
    } else {
        format!("{}-second", number(seconds))
    }
}

/// A number as Python's :g format writes one in Whooper's texts: whole numbers without a fraction.
pub fn number(value: f64) -> String {
    if value.fract() == 0.0 && value.abs() < 1e15 {
        format!("{}", value as i64)
    } else {
        format!("{value}")
    }
}

/// The text after the last of a review's lines that is not blank, or the whole when it is one line.
pub fn last_line(text: &str) -> Option<&str> {
    text.lines()
        .rev()
        .map(str::trim_end)
        .find(|line| !line.trim().is_empty())
}
