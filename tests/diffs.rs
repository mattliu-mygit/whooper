//! Black-box tests of the diff a review keeps: each git setting that would change the patch, and
//! diff.patch and the setup script's list of changed files as git's defaults print them whatever the
//! user's git config or git's diff variables say.

mod common;

use std::fs;
use std::path::PathBuf;

use common::*;
use serde_json::json;

fn app(changed: bool) -> String {
    (1..35)
        .map(|n| {
            if changed && (n == 13 || n == 22) {
                format!("changed {n}\n")
            } else {
                format!("line {n}\n")
            }
        })
        .collect()
}

const GREETING: &str = "hello\nthere\none\ntwo\nthree\nfour\nfive\nsix\nseven\neight\n";
const REPORT_BASE: &str = "// Prints the total of the prices.\nvoid report(int total)\n{\n    if (total > 0)\n    {\n        printf(\"Total: \");\n        printf(\"%d\\n\", total);\n    }\n}\n\nint sum(int n)\n{\n    if (n > 0)\n    {\n        return sum(n - 1) + n;\n    }\n    return 0;\n}\n";
const REPORT_HEAD: &str = "int count(int n)\n{\n    if (n > 0)\n    {\n        return count(n - 1) + 1;\n    }\n    return 0;\n}\n\n// Prints the total of the prices.\nvoid report(int total)\n{\n    if (total > 0)\n    {\n        printf(\"%d\\n\", total);\n    }\n}\n";

/// PR 12 of sample-org/plain-repo: app.txt changes lines 13 and 22 of 34, two hunks by git's defaults;
/// report.c reads otherwise by patience than by myers; notes.txt moves to docs/notes.txt; app-copy.txt
/// is a copy of app.txt as it was; greeting.txt moves to hello.txt with its last line changed; café.txt
/// has a name git quotes; sliders.txt gains 3 lines git could show in more than one place;
/// generated.txt, which .gitattributes marks -diff, changes; and the PR adds the submodule vendor/lib.
/// main moves on after it. Returns the work item's path and the merge base.
fn pull_request(w: &Whooper) -> (PathBuf, String) {
    let origin = w.root().join("origin");
    fs::create_dir_all(&origin).unwrap();
    let write = |files: &[(&str, Option<&str>)]| {
        for (name, text) in files {
            let path = origin.join(name);
            match text {
                Some(t) => {
                    fs::create_dir_all(path.parent().unwrap()).unwrap();
                    fs::write(path, t).unwrap();
                }
                None => {
                    let _ = fs::remove_file(path);
                }
            }
        }
    };
    let (app_base, app_head) = (app(false), app(true));
    let hello = GREETING.replace("eight", "EIGHT");
    w.git(&origin, &["init", "--quiet", "--initial-branch=main"]);
    write(&[
        ("app.txt", Some(&app_base)),
        ("generated.txt", Some("g1\n")),
        (".gitattributes", Some("generated.txt -diff\n")),
        ("report.c", Some(REPORT_BASE)),
        ("notes.txt", Some("first\nsecond\nthird\n")),
        ("greeting.txt", Some(GREETING)),
        ("café.txt", Some("one\n")),
        ("sliders.txt", Some("1\n2\na\n\nb\n3\n4\n")),
    ]);
    w.git(&origin, &["add", "."]);
    w.git(&origin, &["commit", "--quiet", "-m", "base"]);
    let merge_base = w.git(&origin, &["rev-parse", "HEAD"]);
    w.git(&origin, &["checkout", "--quiet", "-b", "pr-12"]);
    write(&[
        ("app.txt", Some(&app_head)),
        ("generated.txt", Some("g2\n")),
        ("report.c", Some(REPORT_HEAD)),
        ("notes.txt", None),
        ("docs/notes.txt", Some("first\nsecond\nthird\n")),
        ("app-copy.txt", Some(&app_base)),
        ("greeting.txt", None),
        ("hello.txt", Some(&hello)),
        ("café.txt", Some("one\ntwo\n")),
        ("sliders.txt", Some("1\n2\na\n\nb\na\n\nb\n3\n4\n")),
    ]);
    w.git(&origin, &["add", "--all"]);
    w.git(
        &origin,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("160000,{},vendor/lib", "1".repeat(40)),
        ],
    );
    w.git(&origin, &["commit", "--quiet", "-m", "the PR"]);
    let head = w.git(&origin, &["rev-parse", "HEAD"]);
    w.git(&origin, &["checkout", "--quiet", "main"]);
    fs::write(origin.join("app.txt"), "one\nlater on main\n").unwrap();
    w.git(&origin, &["commit", "--quiet", "-am", "later on main"]);
    let clone = w.root().join("cache/whooper/repos/sample-org-plain-repo");
    fs::create_dir_all(clone.parent().unwrap()).unwrap();
    w.git(
        &w.root(),
        &[
            "clone",
            "--quiet",
            "--no-checkout",
            origin.to_str().unwrap(),
            clone.to_str().unwrap(),
        ],
    );
    let item = w.root().join("item.json");
    fs::write(
        &item,
        work_item("sample-org/plain-repo", 12, &head).to_string(),
    )
    .unwrap();
    (item, merge_base)
}

/// The git settings, each as the config entries it takes, each of which changes the patch git diff
/// prints of the PR.
fn diff_settings(w: &Whooper) -> Vec<Vec<(&'static str, String)>> {
    let tmp = w.root();
    let (order, attributes) = (tmp.join("order"), tmp.join("attributes"));
    fs::write(&order, "vendor/*\nreport.c\n").unwrap();
    fs::write(&attributes, "app.txt diff=shout\nhello.txt diff=hello\n").unwrap();
    script(&tmp.join("external"), "echo \"external diff of $1\"");
    script(&tmp.join("shout"), "tr a-z A-Z < \"$1\"");
    let s = |p: PathBuf| p.display().to_string();
    let one = |k: &'static str, v: &str| vec![(k, v.to_string())];
    vec![
        one("diff.context", "10"),
        one("diff.interHunkContext", "5"),
        one("diff.algorithm", "patience"),
        vec![("diff.orderFile", s(order))],
        one("diff.renames", "false"),
        one("diff.suppressBlankEmpty", "true"),
        one("core.abbrev", "20"),
        one("core.quotePath", "false"),
        one("diff.ignoreSubmodules", "all"),
        one("diff.renameLimit", "1"),
        vec![
            ("core.attributesFile", s(attributes.clone())),
            ("diff.hello.xfuncname", "^hello$".into()),
        ],
        vec![
            ("core.attributesFile", s(attributes)),
            ("diff.shout.textconv", s(tmp.join("shout"))),
        ],
        vec![("diff.external", s(tmp.join("external")))],
        one("color.ui", "always"),
        one("color.diff", "always"),
        one("diff.noprefix", "true"),
        one("diff.srcPrefix", "SRC/"),
        one("diff.dstPrefix", "DST/"),
        one("diff.submodule", "log"),
        one("core.bigFileThreshold", "10"),
        one("diff.indentHeuristic", "false"),
    ]
}

/// git's own patch of the PR, with review's 12-digit object names, or its list of the files the PR
/// changes, under setting.
fn plain_patch(w: &Whooper, merge_base: &str, setting: &[(&str, String)], names: bool) -> String {
    let mut args = vec!["-c".to_string(), "core.abbrev=12".to_string()];
    for (k, v) in setting {
        args.extend(["-c".to_string(), format!("{k}={v}")]);
    }
    args.push("diff".into());
    if names {
        args.push("--name-only".into());
    }
    args.extend([merge_base.to_string(), "pr-12".to_string()]);
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    // Whooper::git trims the output, so compare with a newline put back.
    format!("{}\n", w.git(&w.root().join("origin"), &args))
}

/// Writes settings as the git config of the tests' home.
fn write_git_config(w: &Whooper, settings: &[Vec<(&str, String)>]) {
    let mut seen = std::collections::BTreeMap::new();
    for (k, v) in settings.iter().flatten() {
        seen.insert(*k, v.clone());
    }
    let mut text = String::new();
    for (key, value) in seen {
        let (section, name) = key.rsplit_once('.').unwrap();
        let header = match section.split_once('.') {
            Some((top, sub)) => format!("[{top} \"{sub}\"]"),
            None => format!("[{section}]"),
        };
        text += &format!("{header}\n\t{name} = {value}\n");
    }
    fs::write(w.home.join(".gitconfig"), text).unwrap();
}

#[test]
fn each_diff_setting_changes_the_patch_of_the_pr() {
    // Without this, the tests below could pass with a setting left as git's default.
    let w = Whooper::new();
    let (_, merge_base) = pull_request(&w);
    let (plain, names) = (
        plain_patch(&w, &merge_base, &[], false),
        plain_patch(&w, &merge_base, &[], true),
    );
    let mut listed = std::collections::BTreeSet::new();
    for setting in diff_settings(&w) {
        assert_ne!(
            plain_patch(&w, &merge_base, &setting, false),
            plain,
            "{setting:?}"
        );
        if plain_patch(&w, &merge_base, &setting, true) != names {
            listed.extend(setting.iter().map(|(k, _)| *k));
        }
    }
    // The ones that change which files git diff --name-only lists, in what order, or how it writes
    // their names.
    assert_eq!(
        listed,
        [
            "core.quotePath",
            "diff.ignoreSubmodules",
            "diff.orderFile",
            "diff.renameLimit",
            "diff.renames"
        ]
        .into()
    );
}

#[test]
fn the_kept_diff_is_a_plain_patch_whatever_the_git_config_or_gits_diff_variables_say() {
    let w = Whooper::new();
    let (item, merge_base) = pull_request(&w);
    let (plain, names) = (
        plain_patch(&w, &merge_base, &[], false),
        plain_patch(&w, &merge_base, &[], true),
    );
    write_git_config(&w, &diff_settings(&w));
    // The setup script keeps the list of changed files it gets, and the review run notes whether git's
    // diff variables reached it.
    w.set_config(
        "[repos.\"sample-org/plain-repo\"]\n",
        "[repos.\"sample-org/plain-repo\"]\nsetup = \"setup.sh\"\n",
    );
    let changed = w.root().join("changed.txt");
    script(
        &w.config.parent().unwrap().join("setup.sh"),
        &format!("cp \"$WHOOPER_CHANGED\" '{}'", changed.display()),
    );
    let seen = w.root().join("variables");
    w.fake_claude(&format!(
        "echo \"[$GIT_EXTERNAL_DIFF][$GIT_DIFF_OPTS]\" > '{}';",
        seen.display()
    ));
    let out = w
        .command(&["review", item.to_str().unwrap()])
        .env("GIT_EXTERNAL_DIFF", w.root().join("external"))
        .env("GIT_DIFF_OPTS", "--unified=10")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let run = PathBuf::from(String::from_utf8_lossy(&out.stdout).trim());
    assert_eq!(fs::read_to_string(run.join("diff.patch")).unwrap(), plain);
    assert_eq!(fs::read_to_string(&changed).unwrap(), names);
    // The review and everything it starts, its steps included, go without git's diff variables.
    assert_eq!(fs::read_to_string(&seen).unwrap(), "[][]\n");
    assert_eq!(
        read_json(&run.join("findings.json"))["status"],
        json!("reviewed")
    );
}
