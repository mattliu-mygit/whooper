//! Black-box tests of whooper step connections on a throwaway origin's PR: name search for Go without
//! gopls, moved code, the 40-node cap, the time limit with a gopls that never answers, the language
//! server's children, and TypeScript 5 and 6 through lib/ts-callers.mjs with the TypeScript page/
//! installs.

mod common;

use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use common::*;
use serde_json::{Value, json};

const ONE_LEVEL: &str =
    "Callers are one level up only: the code that calls those callers is not shown.";
const NO_GOPLS: &str = "Whooper found no gopls on PATH or where go install puts it, go env GOBIN or GOPATH/bin, so the Go links come from name search.";
const NAME_SEARCH: &str = "Name search links every whole-word match of a name, so a link can join code that only shares the name, and it misses calls that do not spell the name out.";
const PATTERNS: &str = "Where neither gopls nor TypeScript read a file, Whooper finds its definitions by a pattern per language and counts each changed line toward the nearest definition above it, so a change between two definitions can count toward the wrong one.";
const REMOVED: &str = "Removed code shows the callers it had at the merge base.";
const TYPESCRIPT_MISSES: &str = "TypeScript leaves out dynamic calls, such as calls through a function value, a value typed any, or a module import() loads at run time, and links a function only to code that calls it, not to code that only names it, such as a list of handlers.";
const JS_METHODS: &str = "In the JavaScript and TypeScript files TypeScript did not read, a change to a method of a class counts toward the class.";
const NO_TYPESCRIPT: &str = "Whooper found no node_modules/typescript in its package or a folder above it, which the repo's setup script would install";

const SHOP: &[(&str, &str)] = &[
    ("go.mod", "module example.com/shop\n\ngo 1.21\n"),
    (
        "prices/prices.go",
        "package prices\n\n// Total adds up the prices.\nfunc Total(prices []int) int {\n\tsum := 0\n\tfor _, p := range prices {\n\t\tsum += p\n\t}\n\treturn sum\n}\n\n// Discount takes a tenth off.\nfunc Discount(total int) int {\n\treturn total - total/10\n}\n",
    ),
    (
        "shop/shop.go",
        "package shop\n\nimport \"example.com/shop/prices\"\n\n// Checkout charges for the cart.\nfunc Checkout(cart []int) int {\n\treturn prices.Discount(prices.Total(cart))\n}\n",
    ),
    (
        "shop/report.go",
        "package shop\n\nimport (\n\t\"fmt\"\n\n\t\"example.com/shop/prices\"\n)\n\n// Report prints the cart's total.\nfunc Report(cart []int) string {\n\treturn fmt.Sprint(prices.Total(cart))\n}\n",
    ),
    (
        "tools/report.py",
        "def summarize(rows):\n    return len(rows)\n",
    ),
    (
        "tools/cli.py",
        "from report import summarize\n\n\ndef main(rows):\n    print(summarize(rows))\n\n\ndef count(rows):\n    # Unlike summarize, this counts empty rows too.\n    return len(rows)\n",
    ),
];

fn shop_pr() -> Vec<(&'static str, &'static str)> {
    over(
        SHOP,
        &[
            (
                "prices/prices.go",
                "package prices\n\n// Total adds up the prices, leaving out refunds.\nfunc Total(prices []int) int {\n\tsum := 0\n\tfor _, p := range prices {\n\t\tif p > 0 {\n\t\t\tsum += p\n\t\t}\n\t}\n\treturn sum\n}\n",
            ),
            (
                "shop/shop.go",
                "package shop\n\nimport \"example.com/shop/prices\"\n\n// Checkout charges for the cart.\nfunc Checkout(cart []int) int {\n\treturn prices.Total(cart)\n}\n",
            ),
            (
                "tools/report.py",
                "def summarize(rows):\n    return len([row for row in rows if row])\n",
            ),
        ],
    )
}

fn node(path: &str, name: &str, kind: &str, line: i64, change: &str, lines_changed: i64) -> Value {
    json!({"id": format!("{path}:{name}"), "name": name, "kind": kind, "path": path, "line": line,
           "change": change, "lines_changed": lines_changed})
}

fn link(from: &str, to: &str, kind: &str, found_by: &str) -> Value {
    json!({"from": from, "to": to, "kind": kind, "found_by": found_by})
}

/// The environment of a step that finds no gopls: none on PATH, and go install puts tools in an empty
/// folder.
fn without_gopls(w: &Whooper) -> Vec<(&'static str, String)> {
    let gobin = w.root().join("gobin");
    fs::create_dir_all(&gobin).unwrap();
    vec![
        (
            "PATH",
            format!("{}:{}", w.fakes.display(), path_without(&["gopls"])),
        ),
        ("GOBIN", gobin.display().to_string()),
    ]
}

fn connections(w: &Whooper, run: &Path, seconds: Option<f64>, env: &[(&str, String)]) -> Value {
    let env: Vec<(&str, &str)> = env.iter().map(|(k, v)| (*k, v.as_str())).collect();
    w.step_file("connections", run, "connections.json", seconds, &env)
        .unwrap()
}

#[test]
fn without_gopls_go_files_link_by_name_search_too() {
    let w = Whooper::new();
    let run = w.run_dir(SHOP, &shop_pr());
    // Name search spans Total to the line before Discount's comment, a removed blank line, so it counts
    // 7 of its lines. Discount is where it was at the merge base, and its caller is the merge base's
    // Checkout. Report, Total's first caller, comes before main, summarize's.
    let graph = connections(&w, &run, None, &without_gopls(&w));
    let ns = "name_search";
    assert_eq!(
        graph,
        json!({
            "nodes": [node("prices/prices.go", "Total", "func", 4, "changed", 7),
                      node("prices/prices.go", "Discount", "func", 13, "removed", 4),
                      node("shop/shop.go", "Checkout", "func", 6, "changed", 2),
                      node("tools/report.py", "summarize", "func", 1, "changed", 2),
                      node("shop/report.go", "Report", "func", 10, "unchanged", 0),
                      node("tools/cli.py", "main", "func", 4, "unchanged", 0)],
            "edges": [link("shop/report.go:Report", "prices/prices.go:Total", "calls", ns),
                      link("shop/shop.go:Checkout", "prices/prices.go:Total", "calls", ns),
                      link("shop/shop.go:Checkout", "prices/prices.go:Discount", "calls", ns),
                      link("tools/cli.py:main", "tools/report.py:summarize", "calls", ns)],
            "limits": [ONE_LEVEL, NO_GOPLS, NAME_SEARCH, PATTERNS, REMOVED],
        })
    );
}

#[test]
fn moved_code_is_removed_and_added_and_code_the_diff_leaves_alone_shows_where_it_is_in_the_head() {
    let w = Whooper::new();
    // move goes from a.py to b.py, and x, which calls it, moves down 2 lines as w above it grows.
    let base = [
        (
            "a.py",
            "def move():\n    return 2\n\n\ndef keep():\n    return 1\n",
        ),
        (
            "c.py",
            "def w():\n    return 1\n\n\ndef x():\n    return move()\n",
        ),
    ];
    let head = [
        ("a.py", "def keep():\n    return 1\n"),
        ("b.py", "def move():\n    return 2\n"),
        (
            "c.py",
            "def w():\n    total = 1\n    total += 1\n    return total\n\n\ndef x():\n    return move()\n",
        ),
    ];
    let run = w.run_dir(&base, &head);
    let ns = "name_search";
    assert_eq!(
        connections(&w, &run, None, &without_gopls(&w)),
        json!({
            "nodes": [node("a.py", "move", "func", 1, "removed", 4),
                      node("b.py", "move", "func", 1, "added", 2),
                      node("c.py", "w", "func", 1, "changed", 4),
                      // Found at the merge base, where it is on line 5, calling the removed move.
                      node("c.py", "x", "func", 7, "unchanged", 0)],
            "edges": [link("c.py:x", "a.py:move", "calls", ns), link("c.py:x", "b.py:move", "calls", ns)],
            "limits": [ONE_LEVEL, NAME_SEARCH, PATTERNS, REMOVED],
        })
    );
}

#[test]
fn the_graph_holds_at_most_40_nodes_the_changed_code_first_then_each_ones_callers_in_turn() {
    let w = Whooper::new();
    // a has 50 callers and b 2. After a and b come a's first caller, b's first, a's second, b's second,
    // and then a's next ones, up to 40 nodes. Name search stops at a's 40th caller.
    let mut callers: String = (0..50)
        .map(|i| format!("def use_a_{i}():\n    return a()\n\n\n"))
        .collect();
    callers += "def use_b_0():\n    return b()\n\n\ndef use_b_1():\n    return b()\n";
    let base = [
        (
            "lib.py",
            "def a():\n    return 1\n\n\ndef b():\n    return 2\n",
        ),
        ("users.py", callers.as_str()),
    ];
    let head = over(
        &base,
        &[(
            "lib.py",
            "def a():\n    return 10\n\n\ndef b():\n    return 20\n",
        )],
    );
    let run = w.run_dir(&base, &head);
    let graph = connections(&w, &run, None, &without_gopls(&w));
    let ids: Vec<&str> = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| n["id"].as_str().unwrap())
        .collect();
    assert_eq!(
        ids[..6],
        [
            "lib.py:a",
            "lib.py:b",
            "users.py:use_a_0",
            "users.py:use_b_0",
            "users.py:use_a_1",
            "users.py:use_b_1"
        ]
    );
    let rest: Vec<String> = (2..36).map(|i| format!("users.py:use_a_{i}")).collect();
    assert_eq!(ids[6..], rest);
    let edges = graph["edges"].as_array().unwrap();
    assert!(
        edges
            .iter()
            .all(|e| ids.contains(&e["from"].as_str().unwrap())
                && ids.contains(&e["to"].as_str().unwrap()))
    );
    assert_eq!(edges.len(), 38);
    assert!(graph["limits"].as_array().unwrap().contains(&json!(
        "Whooper shows 40 of the 44 or more declarations it found, the changed code first, and leaves out the rest and their links."
    )));
}

#[test]
fn more_than_40_changed_declarations_show_the_first_40_without_callers() {
    let w = Whooper::new();
    let lib: String = (0..45)
        .map(|i| format!("def f{i}():\n    return {i}\n\n\n"))
        .collect();
    let users = "def use():\n    return f0()\n";
    let run = w.run_dir(
        &[("users.py", users)],
        &[("lib.py", &lib), ("users.py", users)],
    );
    let graph = connections(&w, &run, None, &without_gopls(&w));
    let ids: Vec<&str> = graph["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| n["id"].as_str().unwrap())
        .collect();
    let want: Vec<String> = (0..40).map(|i| format!("lib.py:f{i}")).collect();
    assert_eq!(ids, want);
    assert!(
        graph["nodes"]
            .as_array()
            .unwrap()
            .iter()
            .all(|n| n["change"] == "added")
    );
    assert_eq!(graph["edges"], json!([]));
    assert!(graph["limits"].as_array().unwrap().contains(&json!(
        "Whooper shows 40 of the 46 declarations it found, the changed code first, and leaves out the rest and their links."
    )));
}

/// The environment of a step that finds the fakes' gopls first.
fn with_fake_gopls(w: &Whooper, body: &str) -> Vec<(&'static str, String)> {
    w.fake("gopls", body);
    vec![(
        "PATH",
        format!("{}:{}", w.fakes.display(), std::env::var("PATH").unwrap()),
    )]
}

const SPLIT_BASE: &[(&str, &str)] = &[
    (
        "a/report.py",
        "def summarize(rows):\n    return len(rows)\n",
    ),
    (
        "b/total.go",
        "package b\n\nfunc Total() int {\n\treturn 1\n}\n",
    ),
];
const SPLIT_HEAD: &[(&str, &str)] = &[
    ("a/report.py", "def summarize(rows):\n    return 0\n"),
    (
        "b/total.go",
        "package b\n\nfunc Total() int {\n\treturn 2\n}\n",
    ),
];

#[test]
fn the_step_stops_at_its_time_limit_and_keeps_what_it_found() {
    let w = Whooper::new();
    // A gopls that reads the step's first request, writes its process ID, and never answers. report.py
    // comes first in the diff, so the step has read it by the limit; gopls never started, so total.go's
    // declarations come from patterns, and the step stops before it looks for callers.
    let pid = w.root().join("gopls.pid");
    let env = with_fake_gopls(
        &w,
        &format!(
            "read -r request\necho $$ > '{}'\nexec sleep 30",
            pid.display()
        ),
    );
    let run = w.run_dir(SPLIT_BASE, SPLIT_HEAD);
    let started = Instant::now();
    let graph = connections(&w, &run, Some(3.0), &env);
    assert!(started.elapsed() < Duration::from_secs(15));
    assert_eq!(
        graph,
        json!({
            "nodes": [node("a/report.py", "summarize", "func", 1, "changed", 2),
                      node("b/total.go", "Total", "func", 3, "changed", 2)],
            "edges": [],
            "limits": [ONE_LEVEL, PATTERNS, "Whooper stopped looking at its 3-second limit, so changed code and callers may be missing."],
        })
    );
    // The step stopped the gopls it started.
    let gopls: i32 = fs::read_to_string(&pid).unwrap().trim().parse().unwrap();
    assert!(ends_within(gopls, 5));
}

/// A fake gopls that starts a child, which ignores TERM and writes a line to a file every tenth of a
/// second, as a go command gopls starts writes its build folder, and then answers initialize and
/// waits, or answers every request with an empty result when it answers. Returns the step's
/// environment, the file the child writes its process ID to, and the file it writes.
fn gopls_with_a_child(
    w: &Whooper,
    answers: bool,
) -> (Vec<(&'static str, String)>, PathBuf, PathBuf) {
    let (child, written) = (w.root().join("child.pid"), w.root().join("written"));
    let answer = |id: &str| {
        let body = format!("{{\"jsonrpc\": \"2.0\", \"id\": {id}, \"result\": null}}");
        format!(
            "printf 'Content-Length: %s\\r\\n\\r\\n%s' {} '{body}'",
            body.len()
        )
    };
    let init = r#"{"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}"#;
    let mut body = vec![
        format!(
            "(trap '' TERM; while :; do echo x >> '{}'; sleep 0.1; done) < /dev/null > /dev/null 2>&1 &",
            written.display()
        ),
        format!("echo $! > '{}'", child.display()),
        format!(
            "printf 'Content-Length: {}\\r\\n\\r\\n%s' '{init}'",
            init.len()
        ),
    ];
    if answers {
        // Reads each request's header and body and answers with its id.
        body.push(format!(
            "while IFS= read -r line; do\n  case \"$line\" in Content-Length:*) n=$(echo \"$line\" | tr -dc 0-9) ;; $(printf '\\r')) req=$(dd bs=1 count=\"$n\" 2>/dev/null); id=$(echo \"$req\" | sed -n 's/.*\"id\": *\\([0-9]*\\).*/\\1/p'); [ -n \"$id\" ] && {} ;; esac\ndone",
            answer("$id")
        ));
    } else {
        body.push("exec sleep 60".into());
    }
    (with_fake_gopls(w, &body.join("\n")), child, written)
}

/// Checks that the child, by the process ID in the file child, ends, and that written, the file it
/// writes, then stops growing.
fn check_stopped(child: &Path, written: &Path) {
    let pid: i32 = fs::read_to_string(child).unwrap().trim().parse().unwrap();
    let ended = ends_within(pid, 5);
    if !ended {
        unsafe { libc::kill(pid, libc::SIGKILL) };
    }
    assert!(ended, "the language server's child still runs");
    let size = fs::metadata(written).map(|m| m.len()).unwrap_or(0);
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(fs::metadata(written).map(|m| m.len()).unwrap_or(0), size);
}

#[test]
fn a_language_server_that_runs_out_of_time_leaves_nothing_it_started_running() {
    let w = Whooper::new();
    let (env, child, written) = gopls_with_a_child(&w, false);
    let run = w.run_dir(SPLIT_BASE, SPLIT_HEAD);
    connections(&w, &run, Some(3.0), &env);
    check_stopped(&child, &written);
}

#[test]
fn a_closed_language_server_leaves_nothing_it_started_running() {
    let w = Whooper::new();
    let (env, child, written) = gopls_with_a_child(&w, true);
    let run = w.run_dir(SPLIT_BASE, SPLIT_HEAD);
    let started = Instant::now();
    connections(&w, &run, Some(60.0), &env);
    // The step ended long before its limit, so it closed the server rather than timing it out.
    assert!(started.elapsed() < Duration::from_secs(30));
    check_stopped(&child, &written);
}

// A TypeScript package's PR: total leaves out refunds, Cart's items become readonly, and Cart.add
// leaves out items that are not above 0. checkout calls total and Cart.add and uses Cart, report calls
// total by an import's alias, handlers names it without calling it, and useIt calls a total of its
// own.
const TS_SHOP: &[(&str, &str)] = &[
    (
        "shop/package.json",
        "{\"name\": \"shop\", \"private\": true, \"devDependencies\": {\"typescript\": \"5.9.3\"}}\n",
    ),
    ("shop/.gitignore", "node_modules/\n"),
    (
        "shop/tsconfig.json",
        "{\"compilerOptions\": {\"strict\": true, \"module\": \"esnext\", \"moduleResolution\": \"bundler\", \"target\": \"es2022\", \"noEmit\": true}, \"include\": [\"src\"]}\n",
    ),
    (
        "shop/src/prices.ts",
        "/** Totals the prices. */\nexport function total(prices: number[]): number {\n  return prices.reduce((sum, p) => sum + p, 0);\n}\n\nexport class Cart {\n  items: number[] = [];\n\n  add(item: number) {\n    this.items.push(item);\n  }\n}\n",
    ),
    (
        "shop/src/checkout.ts",
        "import { Cart, total } from \"./prices\";\n\nexport function checkout(cart: number[]): number {\n  const c = new Cart();\n  c.add(1);\n  return total(cart);\n}\n",
    ),
    (
        "shop/src/report.ts",
        "import { total as sum } from \"./prices\";\n\nexport const report = (rows: number[]) => `Total: ${sum(rows)}`;\n\nexport const handlers = [sum];\n",
    ),
    (
        "shop/src/unrelated.ts",
        "// Another total, which shares only the name.\nfunction total(n: number): number {\n  return n;\n}\n\nexport function useIt(): number {\n  return total(1);\n}\n",
    ),
];
const TS_PRICES_PR: &str = "/** Totals the prices, leaving out refunds. */\nexport function total(prices: number[]): number {\n  return prices.filter((p) => p > 0).reduce((sum, p) => sum + p, 0);\n}\n\nexport class Cart {\n  readonly items: number[] = [];\n\n  add(item: number) {\n    if (item > 0) {\n      this.items.push(item);\n    }\n  }\n}\n";

fn ts_shop_pr() -> Vec<(&'static str, &'static str)> {
    over(TS_SHOP, &[("shop/src/prices.ts", TS_PRICES_PR)])
}

fn ts_node(path: &str, name: &str, kind: &str, line: i64) -> Value {
    node(path, name, kind, line, "unchanged", 0)
}

/// connections.json of TS_SHOP's PR when name search finds its links and patterns its definitions,
/// since TypeScript did not run, for why.
fn ts_shop_by_name_search(why: &str) -> Value {
    let ns = "name_search";
    json!({
        "nodes": [node("shop/src/prices.ts", "total", "func", 2, "changed", 4),
                  node("shop/src/prices.ts", "Cart", "type", 6, "changed", 6),
                  ts_node("shop/src/checkout.ts", "checkout", "func", 3),
                  ts_node("shop/src/unrelated.ts", "total", "func", 2),
                  ts_node("shop/src/unrelated.ts", "useIt", "func", 6)],
        "edges": [link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:total", "calls", ns),
                  link("shop/src/unrelated.ts:total", "shop/src/prices.ts:total", "calls", ns),
                  link("shop/src/unrelated.ts:useIt", "shop/src/prices.ts:total", "calls", ns),
                  link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:Cart", "uses", ns)],
        "limits": [ONE_LEVEL, format!("The links of 1 changed file come from name search, since {why}: shop/src/prices.ts."),
                   NAME_SEARCH, PATTERNS, JS_METHODS],
    })
}

/// connections.json of TS_SHOP's PR when TypeScript finds its links and definitions.
fn ts_shop_by_typescript() -> Value {
    let ts = "typescript";
    json!({
        "nodes": [node("shop/src/prices.ts", "total", "func", 2, "changed", 4),
                  node("shop/src/prices.ts", "Cart", "type", 6, "changed", 2),
                  node("shop/src/prices.ts", "Cart.add", "method", 9, "changed", 4),
                  ts_node("shop/src/checkout.ts", "checkout", "func", 3),
                  ts_node("shop/src/report.ts", "report", "func", 3)],
        "edges": [link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:total", "calls", ts),
                  link("shop/src/report.ts:report", "shop/src/prices.ts:total", "calls", ts),
                  link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:Cart", "uses", ts),
                  link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:Cart.add", "calls", ts)],
        "limits": [ONE_LEVEL, TYPESCRIPT_MISSES],
    })
}

/// page/node_modules, where npm ci in page/ installs TypeScript, or None, with a note, when it has
/// none or node is missing, so the test is skipped.
fn page_modules() -> Option<PathBuf> {
    let modules = Path::new(env!("CARGO_MANIFEST_DIR")).join("page/node_modules");
    let ok = modules.join("typescript/lib/typescript.js").is_file()
        && Command::new("node")
            .arg("--version")
            .stdout(Stdio::null())
            .status()
            .is_ok_and(|s| s.success());
    if !ok {
        eprintln!("skipped: no node or page/node_modules/typescript; npm ci in page/ installs it");
    }
    ok.then_some(modules)
}

/// Installs page's TypeScript in package's node_modules, as the repo's setup script would.
fn install_typescript(package: &Path, modules: &Path) {
    fs::create_dir_all(package.join("node_modules")).unwrap();
    symlink(
        modules.join("typescript"),
        package.join("node_modules/typescript"),
    )
    .unwrap();
}

/// Installs in package's node_modules a typescript package of version whose lib/typescript.js is
/// body, or which has none, as TypeScript 7 has none.
fn fake_typescript(package: &Path, body: Option<&str>, version: &str) {
    let folder = package.join("node_modules/typescript");
    fs::create_dir_all(folder.join("lib")).unwrap();
    fs::write(
        folder.join("package.json"),
        json!({"name": "typescript", "version": version}).to_string(),
    )
    .unwrap();
    if let Some(body) = body {
        fs::write(folder.join("lib/typescript.js"), body).unwrap();
    }
}

/// JavaScript that writes its process ID to the file pid and then waits for ever.
fn block_once(pid: &Path) -> String {
    format!(
        "require(\"fs\").writeFileSync({}, String(process.pid));\nAtomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);\n",
        json!(pid.display().to_string())
    )
}

/// Leaves in the head's checkout what a review run might: a probe that calls total, and a function it
/// added to checkout.ts that calls total too. TypeScript reads neither.
fn leave_a_probe_and_an_edit(head: &Path) {
    fs::write(
        head.join("shop/src/probe.ts"),
        "import { total } from \"./prices\";\n\nexport const probe = () => total([1]);\n",
    )
    .unwrap();
    let checkout = head.join("shop/src/checkout.ts");
    let text = fs::read_to_string(&checkout).unwrap();
    fs::write(
        &checkout,
        format!("// probe\n\n{text}\nexport const extra = () => total([]);\n"),
    )
    .unwrap();
}

#[test]
fn typescript_5_and_6_link_through_ts_callers_to_the_code_that_calls_them() {
    let Some(modules) = page_modules() else {
        return;
    };
    let w = Whooper::new();
    let run = w.run_dir(TS_SHOP, &ts_shop_pr());
    install_typescript(&run.join("head/shop"), &modules);
    leave_a_probe_and_an_edit(&run.join("head"));
    assert_eq!(connections(&w, &run, None, &[]), ts_shop_by_typescript());
}

#[test]
fn a_package_without_typescript_links_by_name_search_and_says_so() {
    let w = Whooper::new();
    let run = w.run_dir(TS_SHOP, &ts_shop_pr());
    assert_eq!(
        connections(&w, &run, None, &[]),
        ts_shop_by_name_search(NO_TYPESCRIPT)
    );
}

#[test]
fn typescript_7_links_by_name_search_and_says_why() {
    let w = Whooper::new();
    let run = w.run_dir(TS_SHOP, &ts_shop_pr());
    fake_typescript(&run.join("head/shop"), None, "7.1.0-beta");
    assert_eq!(
        connections(&w, &run, None, &[]),
        ts_shop_by_name_search(
            "its TypeScript has no lib/typescript.js, the JavaScript API Whooper asks, as TypeScript 7 does not"
        )
    );
}

#[test]
fn typescript_stops_at_its_share_of_the_time_limit_and_name_search_finds_the_rest() {
    let Some(modules) = page_modules() else {
        return;
    };
    let w = Whooper::new();
    let run = w.run_dir(TS_SHOP, &ts_shop_pr());
    // A TypeScript that lists declarations, and that waits for ever once asked to load a project.
    let pid = w.root().join("typescript.pid");
    let body = format!(
        "const ts = require({});\nmodule.exports = new Proxy(ts, {{get(target, key) {{\n  if (key === 'getParsedCommandLineOfConfigFile') {{ {} }}\n  return target[key];\n}}}});",
        json!(modules.join("typescript").display().to_string()),
        block_once(&pid)
    );
    fake_typescript(&run.join("head/shop"), Some(&body), "5.9.3");
    let started = Instant::now();
    let graph = connections(&w, &run, Some(8.0), &[]);
    assert!(started.elapsed() < Duration::from_secs(20));
    // TypeScript listed prices.ts's declarations, so Cart.add is a method of its own, and name search
    // found the callers of each.
    let ns = "name_search";
    assert_eq!(
        graph,
        json!({
            "nodes": [node("shop/src/prices.ts", "total", "func", 2, "changed", 4),
                      node("shop/src/prices.ts", "Cart", "type", 6, "changed", 2),
                      node("shop/src/prices.ts", "Cart.add", "method", 9, "changed", 4),
                      ts_node("shop/src/checkout.ts", "checkout", "func", 3),
                      ts_node("shop/src/unrelated.ts", "total", "func", 2),
                      ts_node("shop/src/unrelated.ts", "useIt", "func", 6)],
            "edges": [link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:total", "calls", ns),
                      link("shop/src/unrelated.ts:total", "shop/src/prices.ts:total", "calls", ns),
                      link("shop/src/unrelated.ts:useIt", "shop/src/prices.ts:total", "calls", ns),
                      link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:Cart", "uses", ns),
                      link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:Cart.add", "calls", ns)],
            "limits": [ONE_LEVEL, "The links of 1 changed file come from name search, since TypeScript's 6-second share of the step's 8-second limit ran out: shop/src/prices.ts.",
                       NAME_SEARCH, PATTERNS, JS_METHODS],
        })
    );
    // The step stopped the TypeScript it started.
    let node: i32 = fs::read_to_string(&pid).unwrap().trim().parse().unwrap();
    assert!(ends_within(node, 5));
}

#[test]
fn moved_typescript_code_shows_the_callers_typescript_finds_at_each_commit() {
    let Some(modules) = page_modules() else {
        return;
    };
    let w = Whooper::new();
    // discount moves from prices.ts to deals.ts, so checkout.ts imports it from there, and checkout,
    // which calls it and does not change, moves down a line. unrelated.ts has a discount of its own.
    // The package's tsconfig.json only references the project of its code, as a solution does.
    let discount = "/** Takes a tenth off. */\nexport function discount(amount: number): number {\n  return amount - amount / 10;\n}\n";
    let get = |path: &str| TS_SHOP.iter().find(|(p, _)| *p == path).unwrap().1;
    let prices = format!("{}\n{discount}", get("shop/src/prices.ts"));
    let unrelated = format!(
        "{}\nfunction discount(n: number): number {{\n  return n;\n}}\n\nexport const useDiscount = () => discount(2);\n",
        get("shop/src/unrelated.ts")
    );
    let checkout = "import { Cart, discount, total } from \"./prices\";\n\nexport function checkout(cart: number[]): number {\n  const c = new Cart();\n  c.add(1);\n  return discount(total(cart));\n}\n";
    let base = over(
        TS_SHOP,
        &[
            (
                "shop/tsconfig.json",
                "{\"files\": [], \"references\": [{\"path\": \"./tsconfig.app.json\"}]}\n",
            ),
            ("shop/tsconfig.app.json", get("shop/tsconfig.json")),
            ("shop/src/prices.ts", &prices),
            ("shop/src/checkout.ts", checkout),
            ("shop/src/unrelated.ts", &unrelated),
        ],
    );
    let moved = checkout.replace(
        "import { Cart, discount, total } from \"./prices\";",
        "import { discount } from \"./deals\";\nimport { Cart, total } from \"./prices\";",
    );
    let head = over(
        &base,
        &[
            ("shop/src/prices.ts", get("shop/src/prices.ts")),
            ("shop/src/deals.ts", discount),
            ("shop/src/checkout.ts", &moved),
        ],
    );
    let run = w.run_dir(&base, &head);
    install_typescript(&run.join("head/shop"), &modules);
    let ts = "typescript";
    assert_eq!(
        connections(&w, &run, None, &[]),
        json!({
            "nodes": [node("shop/src/deals.ts", "discount", "func", 2, "added", 4),
                      node("shop/src/prices.ts", "discount", "func", 15, "removed", 4),
                      // Found at the merge base, where it is on line 3, calling the removed discount.
                      ts_node("shop/src/checkout.ts", "checkout", "func", 4)],
            "edges": [link("shop/src/checkout.ts:checkout", "shop/src/deals.ts:discount", "calls", ts),
                      link("shop/src/checkout.ts:checkout", "shop/src/prices.ts:discount", "calls", ts)],
            "limits": [ONE_LEVEL, TYPESCRIPT_MISSES, REMOVED],
        })
    );
}

#[test]
fn a_stopped_step_leaves_no_typescript_running() {
    if page_modules().is_none() {
        return;
    }
    let w = Whooper::new();
    let run = w.run_dir(TS_SHOP, &ts_shop_pr());
    let pid = w.root().join("typescript.pid");
    fake_typescript(&run.join("head/shop"), Some(&block_once(&pid)), "5.9.3");
    // review stops a step with a TERM to the step's process group, which node, in a group of its own,
    // is not in.
    let mut command = w.command(&["step", "connections", run.to_str().unwrap()]);
    std::os::unix::process::CommandExt::process_group(&mut command, 0);
    let mut step = command
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    wait_for("TypeScript to start", || {
        fs::read_to_string(&pid).is_ok_and(|t| !t.is_empty())
    });
    let node: i32 = fs::read_to_string(&pid).unwrap().trim().parse().unwrap();
    unsafe { libc::killpg(step.id() as i32, libc::SIGTERM) };
    step.wait().unwrap();
    let ended = ends_within(node, 5);
    if !ended {
        unsafe { libc::kill(node, libc::SIGKILL) };
    }
    assert!(ended, "TypeScript still runs");
}
