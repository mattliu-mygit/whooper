//! Process control: the stop flag that INT, TERM, and HUP set, children started in sessions of their
//! own, time limits, and stopping a child's whole process tree.
//!
//! Every program Whooper starts runs in a session of its own, so it and everything it starts form one
//! process group, and none of it reads the terminal. When the program ends, passes its time limit, or
//! the command that started it stops, its tree is stopped (stop_tree): a TERM to its group, then, 4
//! seconds later, a SIGKILL to whatever is left of it and of every group below it, so no part of a
//! review, such as a server a probe started, keeps running.

use std::collections::BTreeSet;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

static STOP: std::sync::OnceLock<Arc<AtomicBool>> = std::sync::OnceLock::new();

const SIGNALS: [i32; 3] = [libc::SIGINT, libc::SIGTERM, libc::SIGHUP];

/// Turns INT, TERM, and HUP into the stop flag, so every wait wakes up and the command cleans up in
/// its normal control flow.
pub fn catch_signals() {
    let flag = STOP.get_or_init(|| Arc::new(AtomicBool::new(false)));
    for signal in SIGNALS {
        let _ = signal_hook::flag::register(signal, flag.clone());
    }
}

/// Whether a signal asked this command to stop.
pub fn stopping() -> bool {
    STOP.get().is_some_and(|f| f.load(Ordering::SeqCst))
}

/// Ignores INT, TERM, and HUP from now on, for cleanup that must not be cut short, such as removing a
/// lock. The stop flag stays set.
pub fn ignore_signals() {
    for signal in SIGNALS {
        unsafe { libc::signal(signal, libc::SIG_IGN) };
    }
}

/// The error a command returns when a signal stopped it: exit code 130, as a shell's.
pub fn stopped() -> crate::fsx::Exit {
    crate::fsx::Exit::new(130, "")
}

/// Fails with stopped once a signal asked this command to stop.
pub fn check() -> crate::fsx::Result<()> {
    if stopping() {
        return Err(stopped());
    }
    Ok(())
}

/// Starts command in a session of its own, with no input unless the caller set one, so it and
/// everything it starts form one process group that stop_tree stops whole.
pub fn spawn(command: &mut Command) -> std::io::Result<Child> {
    unsafe {
        command.pre_exec(|| {
            libc::setsid();
            // The child starts with the default handlers, not ignored signals of a cleanup.
            for signal in SIGNALS {
                libc::signal(signal, libc::SIG_DFL);
            }
            Ok(())
        });
    }
    command.spawn()
}

/// How a wait ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ended {
    Exited(i32),
    TimedOut,
    Stopped,
}

/// The exit code of status, with 128 plus the signal for a program a signal ended.
pub fn code(status: ExitStatus) -> i32 {
    status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(0))
}

/// Whether child has ended, without reaping it, so its process group ID stays reserved until
/// stop_tree has stopped what is left of the group.
fn ended(child: &Child) -> bool {
    let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
    let r = unsafe {
        libc::waitid(
            libc::P_PID,
            child.id(),
            &mut info,
            libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
        )
    };
    r != 0 || unsafe { info.si_pid() } != 0
}

/// Waits for child until it exits, passes limit, or a signal asks this command to stop (unless
/// ignore_stop), and stops its whole tree before returning, whatever happened.
pub fn wait_with(child: &mut Child, limit: Option<Duration>, ignore_stop: bool) -> Ended {
    let deadline = limit.map(|l| Instant::now() + l);
    let how = loop {
        if ended(child) {
            break None;
        }
        if !ignore_stop && stopping() {
            break Some(Ended::Stopped);
        }
        if deadline.is_some_and(|d| Instant::now() >= d) {
            break Some(Ended::TimedOut);
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    stop_tree(child.id() as i32);
    let status = child.wait();
    match how {
        Some(how) => how,
        None => Ended::Exited(status.map(code).unwrap_or(1)),
    }
}

/// wait_with that a signal to this command ends.
pub fn wait(child: &mut Child, limit: Option<Duration>) -> Ended {
    wait_with(child, limit, false)
}

/// What run_captured returns: the exit code, or None once the command passed its limit or this
/// command was stopped, with its output and error output.
pub struct Captured {
    pub code: Option<i32>,
    pub ended: Ended,
    pub out: String,
    pub err: String,
}

/// Runs command in a session of its own with no input and its output captured.
pub fn run_captured(command: &mut Command, limit: Option<Duration>) -> std::io::Result<Captured> {
    use std::io::{Read, Seek};
    let out = tempfile::tempfile()?;
    let err = tempfile::tempfile()?;
    command
        .stdin(Stdio::null())
        .stdout(out.try_clone()?)
        .stderr(err.try_clone()?);
    let mut child = spawn(command)?;
    let ended = wait(&mut child, limit);
    let read = |mut f: std::fs::File| -> String {
        let mut bytes = Vec::new();
        let _ = f.rewind();
        let _ = f.read_to_end(&mut bytes);
        String::from_utf8_lossy(&bytes).into_owned()
    };
    Ok(Captured {
        code: match ended {
            Ended::Exited(c) => Some(c),
            _ => None,
        },
        ended,
        out: read(out),
        err: read(err),
    })
}

/// Runs command as run_captured does, without a limit, and fails with stopped when a signal stopped
/// this command meanwhile.
pub fn output(command: &mut Command) -> crate::fsx::Result<Captured> {
    let captured = run_captured(command, None)?;
    if captured.ended == Ended::Stopped {
        return Err(stopped());
    }
    Ok(captured)
}

fn alive(group: i32) -> bool {
    unsafe { libc::kill(-group, 0) == 0 }
}

/// The process groups below groups: the group of each process in them and of each process those
/// started, however deep, by parent process ID, leaving out the groups of this process and its
/// ancestors, so a reused group ID never stops this command or its caller.
fn below(groups: &BTreeSet<i32>) -> BTreeSet<i32> {
    let Ok(out) = Command::new("/bin/ps")
        .args(["-A", "-o", "pid=", "-o", "ppid=", "-o", "pgid="])
        .output()
    else {
        return groups.clone();
    };
    let rows: Vec<(i32, i32, i32)> = String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|l| {
            let f: Vec<i32> = l
                .split_whitespace()
                .filter_map(|x| x.parse().ok())
                .collect();
            (f.len() == 3).then(|| (f[0], f[1], f[2]))
        })
        .collect();
    let mut under: BTreeSet<i32> = rows
        .iter()
        .filter(|r| groups.contains(&r.2))
        .map(|r| r.0)
        .collect();
    loop {
        let more: Vec<i32> = rows
            .iter()
            .filter(|r| !under.contains(&r.0) && under.contains(&r.1))
            .map(|r| r.0)
            .collect();
        if more.is_empty() {
            break;
        }
        under.extend(more);
    }
    let mut mine = BTreeSet::new();
    let mut p = std::process::id() as i32;
    while p > 1 {
        let Some(row) = rows.iter().find(|r| r.0 == p) else {
            break;
        };
        mine.insert(row.2);
        p = row.1;
    }
    rows.iter()
        .filter(|r| under.contains(&r.0) && !mine.contains(&r.2))
        .map(|r| r.2)
        .collect()
}

/// Stops the process group group and every group below it: a TERM to the group, up to 4 seconds for
/// it all to end, under the 5 Prompting Time gives whooper run between its TERM and its SIGKILL, then
/// SIGSTOP to what is left until no new group appears, and a SIGKILL to all of it, and up to 2 seconds
/// more for it to end.
pub fn stop_tree(group: i32) {
    if group <= 1 {
        return;
    }
    let mut groups = below(&BTreeSet::from([group]));
    groups.insert(group);
    let left = |groups: &BTreeSet<i32>| groups.iter().any(|&g| alive(g));
    if !left(&groups) {
        return;
    }
    unsafe { libc::kill(-group, libc::SIGTERM) };
    let deadline = Instant::now() + Duration::from_secs(4);
    while left(&groups) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(50));
    }
    if !left(&groups) {
        return;
    }
    loop {
        for &g in &groups {
            unsafe { libc::kill(-g, libc::SIGSTOP) };
        }
        let mut found = below(&groups);
        found.extend(groups.iter().copied());
        if found == groups {
            break;
        }
        groups = found;
    }
    for &g in &groups {
        unsafe { libc::kill(-g, libc::SIGKILL) };
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while left(&groups) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// Forgets a stop a signal asked for, so the waits that follow, such as a cleanup's teardown script,
/// stop only at a new signal.
pub fn clear() {
    if let Some(flag) = STOP.get() {
        flag.store(false, Ordering::SeqCst);
    }
}
