//! Whooper's locks: run.lock and round.lock in the state folder, and one lock a PR under locks/, each a
//! folder holding the process ID of the command that holds it. mkdir is atomic, so of two commands
//! started at once only one gets the lock. A lock whose process is gone, or now runs another program,
//! is stale, and the next command takes it over. Commands hold an exclusive flock on <state>/locks
//! while they make a lock or take one over, so no command reads a lock another is still making.

use std::fs;
use std::os::fd::AsRawFd;
use std::path::Path;
use std::process::Command;
use std::time::{Duration, Instant};

use crate::fsx;

/// Whether process pid runs the Whooper command named, such as whooper-round, by its command line as
/// ps prints it: bin/whooper-round, or bin/whooper round. Both spellings count, so a lock taken by a
/// copy of Whooper from before its commands became subcommands still counts while that copy runs.
pub fn runs(pid: i64, command: &str) -> bool {
    let Ok(out) = Command::new("/bin/ps")
        .args(["-o", "command=", "-p", &pid.to_string()])
        .output()
    else {
        return false;
    };
    let line = String::from_utf8_lossy(&out.stdout);
    let spaced = command.replacen('-', " ", 1);
    line.contains(command) || line.contains(&spaced)
}

/// The process ID in a lock's pid file, or None while it has none.
pub fn pid_in(lock: &Path) -> Option<i64> {
    fs::read_to_string(lock.join("pid"))
        .ok()?
        .trim()
        .parse()
        .ok()
}

/// The process that holds lock, when it is live and runs command; None for a stale lock or none.
pub fn holder(lock: &Path, command: &str) -> Option<i64> {
    pid_in(lock).filter(|&pid| runs(pid, command))
}

/// An exclusive or shared flock on a folder, held while the value lives.
pub struct Flock(#[allow(dead_code)] fs::File);

fn flock(folder: &Path, operation: i32) -> std::io::Result<Flock> {
    let file = fs::File::open(folder)?;
    if unsafe { libc::flock(file.as_raw_fd(), operation) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(Flock(file))
}

/// The outcome of take.
pub enum Taken {
    Mine,
    HeldBy(i64),
}

/// Takes the lock folder for this process, writing its pid with fill's other files first, under the
/// flock on <state>/locks: a lock held by a live process running command refuses with that process's
/// ID, and a stale one is taken over. fill writes the lock's other files before the pid, so a lock
/// whose pid names a process has all of its files.
pub fn take(
    state: &Path,
    lock: &Path,
    command: &str,
    fill: impl Fn(&Path) -> std::io::Result<()>,
) -> fsx::Result<Taken> {
    let locks = state.join("locks");
    fs::create_dir_all(&locks)?;
    let _guard = flock(&locks, libc::LOCK_EX)?;
    let attempt = || -> std::io::Result<bool> {
        match fs::create_dir(lock) {
            Ok(()) => {
                fill(lock)?;
                fsx::write_file(
                    &lock.join("pid"),
                    &format!("{}\n", std::process::id()),
                    false,
                )?;
                Ok(true)
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => Ok(false),
            Err(e) => Err(e),
        }
    };
    if attempt()? {
        return Ok(Taken::Mine);
    }
    if let Some(pid) = holder(lock, command) {
        return Ok(Taken::HeldBy(pid));
    }
    let _ = fs::remove_dir_all(lock);
    if attempt()? {
        return Ok(Taken::Mine);
    }
    Err(fsx::Exit::new(
        1,
        format!("{command}: could not take over {}", lock.display()),
    ))
}

/// Removes lock when this process holds it.
pub fn release(lock: &Path) {
    if pid_in(lock) == Some(std::process::id() as i64) {
        let _ = fs::remove_dir_all(lock);
    }
}

/// Holds a shared flock on <state>/locks for reading run.lock and round.lock, so a lock read under it
/// has its pid, or lost it to a process killed while making it, which is stale. It waits at most 2
/// seconds, as making a lock takes milliseconds and one stopped meanwhile holds the flock until it
/// goes on, then reads without it. Without the folder it reads at once.
pub fn reading(state: &Path) -> Option<Flock> {
    let locks = state.join("locks");
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match flock(&locks, libc::LOCK_SH | libc::LOCK_NB) {
            Ok(held) => return Some(held),
            Err(e) if e.raw_os_error() == Some(libc::EWOULDBLOCK) && Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(20))
            }
            Err(_) => return None,
        }
    }
}
