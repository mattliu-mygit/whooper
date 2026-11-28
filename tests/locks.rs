//! Black-box tests of the races around Whooper's locks: run.lock, round.lock, and a PR's lock under
//! locks/. Each command makes or takes over a lock under an exclusive flock on <state>/locks, which the
//! test holds itself to stand for a copy that is still making its lock, so the race happens every time.

mod common;

use std::fs;
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Stdio};
use std::time::Duration;

use common::*;

/// An exclusive flock on <state>/locks, held while the value lives, as a command holds it while it
/// makes its lock.
struct Making(#[allow(dead_code)] fs::File);

fn making(w: &Whooper) -> Making {
    let locks = w.state.join("locks");
    fs::create_dir_all(&locks).unwrap();
    let file = fs::File::open(&locks).unwrap();
    assert_eq!(unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) }, 0);
    Making(file)
}

/// Each command that takes a lock: its arguments, its lock, and the refusal it prints while the
/// process whose ID fills the braces holds it.
fn lock_takers(w: &Whooper, item: &Path) -> Vec<(Vec<String>, PathBuf, String)> {
    vec![
        (
            vec!["run".into()],
            w.state.join("run.lock"),
            format!(
                "whooper-run: process {{}} is already running Whooper; see {}/pid",
                w.state.join("run.lock").display()
            ),
        ),
        (
            vec!["round".into()],
            w.state.join("round.lock"),
            format!(
                "whooper-round: a round is already running, in process {{}}; see {}/pid",
                w.state.join("round.lock").display()
            ),
        ),
        (
            vec!["review".into(), item.display().to_string()],
            w.state.join("locks/sample-org-plain-repo-12"),
            "whooper-review: process {} is already reviewing sample-org/plain-repo#12".into(),
        ),
    ]
}

/// A Whooper whose commands, once they hold their lock, wait until the test stops them: gh, which a
/// round's fetch runs, and claude, which a review runs, wait; and the work item of PR 12.
fn waiting_whooper() -> (Whooper, PathBuf) {
    let w = Whooper::new();
    let (item, _) = w.pull_request(&[("a.txt", "1\n")], &[("a.txt", "2\n")]);
    w.fake("gh", "exec sleep 600");
    w.fake("claude", "exec sleep 600");
    (w, item)
}

fn start(w: &Whooper, args: &[String]) -> Child {
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    w.command(&args)
        .process_group(0)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap()
}

/// Stops a command started by start, and whatever it started in its group, with a TERM.
fn stop(child: &mut Child) {
    unsafe { libc::killpg(child.id() as i32, libc::SIGTERM) };
    let _ = child.wait();
}

fn ended(child: &mut Child) -> Option<(Option<i32>, String)> {
    let status = child.try_wait().unwrap()?;
    let mut err = String::new();
    use std::io::Read;
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut err)
        .unwrap();
    Some((status.code(), err))
}

fn pid_file(lock: &Path) -> Option<String> {
    fs::read_to_string(lock.join("pid")).ok()
}

#[test]
fn a_copy_that_comes_while_another_makes_the_lock_waits_for_it_and_refuses() {
    let (w, item) = waiting_whooper();
    for (args, lock, refusal) in lock_takers(&w, &item) {
        let mut holder = w.holder(&format!("whooper-{}", args[0]));
        // Another copy has made the lock and not yet written its process ID there.
        let flock = making(&w);
        fs::create_dir_all(&lock).unwrap();
        let mut copy = start(&w, &args);
        std::thread::sleep(Duration::from_millis(500));
        // Without the wait, the copy would take over a lock that has no process ID.
        assert!(copy.try_wait().unwrap().is_none(), "{args:?} did not wait");
        fs::write(lock.join("pid"), format!("{}\n", holder.id())).unwrap();
        drop(flock);
        wait_for("the copy to refuse", || copy.try_wait().unwrap().is_some());
        let (code, err) = ended(&mut copy).unwrap();
        assert_eq!(
            (code, err.trim_end()),
            (
                Some(1),
                refusal.replace("{}", &holder.id().to_string()).as_str()
            ),
            "{args:?}"
        );
        assert_eq!(pid_file(&lock), Some(format!("{}\n", holder.id())));
        holder.kill().unwrap();
        holder.wait().unwrap();
        fs::remove_dir_all(&lock).unwrap();
    }
}

#[test]
fn of_two_copies_that_find_the_same_stale_lock_one_takes_it_over_and_the_other_refuses() {
    let (w, item) = waiting_whooper();
    let mut gone = std::process::Command::new("true").spawn().unwrap();
    let gone = {
        gone.wait().unwrap();
        gone.id()
    };
    for (args, lock, refusal) in lock_takers(&w, &item) {
        w.hold(&lock, gone);
        // Both copies come to the lock while a third makes another, so they meet at it.
        let flock = making(&w);
        let mut copies = [start(&w, &args), start(&w, &args)];
        std::thread::sleep(Duration::from_millis(500));
        drop(flock);
        wait_for("one copy to refuse", || {
            copies.iter_mut().any(|c| c.try_wait().unwrap().is_some())
        });
        let refused = if copies[0].try_wait().unwrap().is_some() {
            0
        } else {
            1
        };
        let (code, err) = ended(&mut copies[refused]).unwrap();
        let taker = copies[1 - refused].id();
        assert_eq!(
            (code, err.trim_end()),
            (Some(1), refusal.replace("{}", &taker.to_string()).as_str()),
            "{args:?}"
        );
        assert_eq!(pid_file(&lock), Some(format!("{taker}\n")), "{args:?}");
        stop(&mut copies[1 - refused]);
        let _ = fs::remove_dir_all(&lock);
    }
}

#[test]
fn takes_over_a_lock_without_a_process_id_that_no_copy_is_making() {
    // A copy killed with kill -9 after it made its lock, before it wrote its process ID there, leaves
    // the lock without one, while its flock on <state>/locks ends with it.
    let (w, item) = waiting_whooper();
    for (args, lock, _) in lock_takers(&w, &item) {
        fs::create_dir_all(&lock).unwrap();
        let mut copy = start(&w, &args);
        let mine = format!("{}\n", copy.id());
        wait_for("the copy to take the lock or stop", || {
            pid_file(&lock).as_deref() == Some(mine.as_str()) || copy.try_wait().unwrap().is_some()
        });
        let holder = pid_file(&lock);
        stop(&mut copy);
        assert_eq!(holder, Some(mine), "{args:?}");
        let _ = fs::remove_dir_all(&lock);
    }
}

#[test]
fn status_reads_a_lock_another_command_is_making_once_it_is_made() {
    let w = Whooper::new();
    let lock = w.state.join("round.lock");
    let mut holder = w.holder("whooper-round");
    let flock = making(&w);
    fs::create_dir_all(&lock).unwrap();
    fs::write(
        lock.join("round.json"),
        r#"{"started_at": 1943194837, "selected": null, "reviewing": null, "reviews": []}"#,
    )
    .unwrap();
    let status = w
        .command(&["status"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    std::thread::sleep(Duration::from_millis(300));
    fs::write(lock.join("pid"), format!("{}\n", holder.id())).unwrap();
    drop(flock);
    let out = status.wait_with_output().unwrap();
    let status: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(status["round"]["running"], true, "{}", status["round"]);
    assert!(!status["round"]["started_at"].is_null());
    holder.kill().unwrap();
    holder.wait().unwrap();
}
