//! Whooper: finds the pull requests that are the user's review work, reviews each with an agent, and
//! answers Prompting Time's plugin actions. One program; each command is a subcommand, such as
//! `whooper status`.

mod agent;
mod ask;
mod config;
mod connections;
mod diff;
mod examples;
mod fsx;
mod gaps;
mod gh;
mod lock;
mod poll;
mod proc;
mod prompt;
mod review;
mod round;
mod schema;
mod select;
mod status;
mod submit;
mod work;

use std::ffi::OsString;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

use fsx::Result;

#[derive(Parser)]
#[command(
    name = "whooper",
    about = "Reviews the pull requests that are your review work"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Prints Whooper's status as JSON.
    Status,
    /// Prints one review run as JSON.
    Show { run_id: String },
    /// Prints a review's drafts, or changes them: include, leave-out, text, approve, comment, message.
    Draft {
        #[arg(allow_hyphen_values = true, num_args = 0..)]
        args: Vec<String>,
    },
    /// Posts a review's drafts to GitHub as the user.
    Submit {
        run_id: String,
        #[arg(long)]
        print: bool,
        #[arg(long)]
        allow_mentions: bool,
        #[arg(long)]
        approve_anyway: bool,
        #[arg(long)]
        expect: Option<String>,
    },
    /// Queues another review of a run's PR for the next round.
    Retry {
        #[arg(long = "item")]
        item: Option<String>,
        run_id: Option<String>,
    },
    /// Marks a review skipped, or with --undo unmarks it.
    Skip {
        #[arg(long = "item")]
        item: Option<String>,
        run_id: Option<String>,
        #[arg(long)]
        undo: bool,
    },
    /// Marks a review removed from the lists, or with --undo unmarks it.
    Remove {
        #[arg(long = "item")]
        item: Option<String>,
        run_id: Option<String>,
        #[arg(long)]
        undo: bool,
    },
    /// Pauses whooper-run's scheduled rounds.
    Pause,
    /// Resumes whooper-run's scheduled rounds.
    Resume,
    /// Prints or saves the settings a settings form edits: get, or set <json>.
    Settings {
        #[arg(num_args = 0..)]
        args: Vec<String>,
    },
    /// Runs rounds on the config's [schedule] until stopped.
    Run,
    /// Runs one round now: fetch, poll, select, and a review of each selected item. Posts nothing.
    Round,
    /// Answers a question about a review: <run-id> <question> [--finding <finding-id>].
    Ask {
        #[arg(allow_hyphen_values = true, num_args = 0..)]
        args: Vec<String>,
    },
    /// Writes the user's open GitHub review requests to <state>/poll/github.json.
    #[command(hide = true)]
    Fetch,
    /// Lists the review work in <state>/poll/<time>.json and prints its path.
    #[command(hide = true)]
    Poll,
    /// Prints the work items a round starts, one JSON object a line.
    #[command(hide = true)]
    Select,
    /// Reviews one work item and prints its run directory, as for a replay with --as-of.
    #[command(hide = true)]
    Review {
        item: std::path::PathBuf,
        #[arg(long = "as-of")]
        as_of: Option<String>,
        #[arg(long = "dry-run", hide = true)]
        dry_run: bool,
    },
    /// Runs one of a review's steps on its run directory: connections, test-gaps, examples, or mutation.
    #[command(hide = true)]
    Step {
        name: String,
        run: std::path::PathBuf,
    },
}

fn run(cli: Cli) -> Result<()> {
    let one = |item: Option<String>, run_id: Option<String>, usage: &str| -> Result<String> {
        match (item, run_id) {
            (Some(id), None) | (None, Some(id)) => Ok(id),
            _ => Err(fsx::Exit::new(1, usage.to_string())),
        }
    };
    match cli.command {
        Command::Settings { args } => {
            let config = config::load()?;
            match args.as_slice() {
                [get] if get == "get" => {
                    println!("{}", fsx::pretty(&config::read_settings(&config.values)?))
                }
                [set, values] if set == "set" => {
                    let values: serde_json::Value = serde_json::from_str(values).map_err(|e| {
                        fsx::Exit::new(1, format!("whooper-settings: the values are not JSON: {e}"))
                    })?;
                    config::write_settings(&values)?;
                }
                _ => fail!("usage: whooper settings get | set <json>"),
            }
            return Ok(());
        }
        Command::Run => return round::run(),
        Command::Round => return round::round(),
        Command::Ask { args } => return ask::ask(&args),
        Command::Review { item, as_of, .. } => {
            proc::catch_signals();
            return review::review(&item, as_of.as_deref());
        }
        Command::Step { name, run } => {
            proc::catch_signals();
            let Some(step) = review::steps().get(name.as_str()).copied() else {
                fail!("whooper: no step {name}")
            };
            return step(&run);
        }
        Command::Fetch => {
            proc::catch_signals();
            return poll::fetch(&config::Whooper::load()?);
        }
        Command::Poll => {
            proc::catch_signals();
            return poll::poll(&config::Whooper::load()?);
        }
        Command::Select => {
            let whooper = config::Whooper::load()?;
            for item in select::select(&whooper.state, config::table(&whooper.values, "repos"))? {
                println!("{}", serde_json::to_string(&item)?);
            }
            return Ok(());
        }
        command => {
            config::load_checked()?;
            let state = config::state_dir();
            match command {
                Command::Status => println!("{}", fsx::pretty(&status::read_status(&state)?)),
                Command::Show { run_id } => {
                    println!("{}", fsx::pretty(&status::show_review(&state, &run_id)?))
                }
                Command::Draft { args } => submit::draft(&state, &args)?,
                Command::Submit {
                    run_id,
                    print,
                    allow_mentions,
                    approve_anyway,
                    expect,
                } => submit::submit(
                    &state,
                    &run_id,
                    &submit::SubmitFlags {
                        print,
                        allow_mentions,
                        approve_anyway,
                        expect,
                    },
                )?,
                Command::Retry { item, run_id } => println!(
                    "{}",
                    work::retry(
                        &state,
                        &one(item, run_id, "usage: whooper retry [--item] <run-id>")?
                    )?
                ),
                Command::Skip { item, run_id, undo } => {
                    println!(
                        "{}",
                        work::skip(
                            &state,
                            &one(
                                item,
                                run_id,
                                "usage: whooper skip [--item] <run-id> [--undo]"
                            )?,
                            undo
                        )?
                    )
                }
                Command::Remove { item, run_id, undo } => {
                    println!(
                        "{}",
                        work::remove(
                            &state,
                            &one(
                                item,
                                run_id,
                                "usage: whooper remove [--item] <run-id> [--undo]"
                            )?,
                            undo
                        )?
                    )
                }
                Command::Pause => {
                    std::fs::create_dir_all(&state)?;
                    std::fs::write(state.join("paused"), "")?;
                }
                Command::Resume => {
                    let _ = std::fs::remove_file(state.join("paused"));
                }
                _ => unreachable!(),
            }
        }
    }
    Ok(())
}

/// The usage line each command prints for arguments it does not take.
fn usage(command: &str) -> Option<&'static str> {
    Some(match command {
        "status" => "usage: whooper status",
        "show" => "usage: whooper show <run-id>",
        "draft" => submit::DRAFT_USAGE,
        "submit" => {
            "whooper-submit: usage: whooper submit <run-id> [--print] [--allow-mentions] [--approve-anyway] [--expect <fingerprint>]"
        }
        "retry" => "usage: whooper retry [--item] <run-id>",
        "skip" => "usage: whooper skip [--item] <run-id> [--undo]",
        "remove" => "usage: whooper remove [--item] <run-id> [--undo]",
        "settings" => "usage: whooper settings get | set <json>",
        "review" => "whooper-review: usage: whooper review <work-item.json> [--as-of <time>]",
        _ => return None,
    })
}

fn main() -> ExitCode {
    let args: Vec<OsString> = std::env::args_os().collect();
    let cli = match Cli::try_parse_from(&args) {
        Ok(cli) => cli,
        Err(e) if e.use_stderr() => {
            match args.get(1).and_then(|c| usage(&c.to_string_lossy())) {
                Some(line) => eprintln!("{line}"),
                None => {
                    let _ = e.print();
                }
            }
            return ExitCode::from(1);
        }
        Err(e) => {
            let _ = e.print();
            return ExitCode::SUCCESS;
        }
    };
    match run(cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(exit) => {
            if !exit.message.is_empty() {
                eprintln!("{}", exit.message);
            }
            ExitCode::from(exit.code as u8)
        }
    }
}
