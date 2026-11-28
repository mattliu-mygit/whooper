//! whooper-ask: answers a question about a review with an agent that may read the review's record.

use std::path::{Path, PathBuf};

use crate::fsx;

/// whooper-ask's saved answers about the review in run, asks/1.json, asks/2.json, and so on, in
/// number order.
pub fn ask_files(run: &Path) -> Vec<PathBuf> {
    let mut found: Vec<(u64, PathBuf)> = fsx::entries(&run.join("asks"))
        .into_iter()
        .filter_map(|p| {
            let name = fsx::name_of(&p);
            let stem = name.strip_suffix(".json")?;
            if stem.is_empty() || !stem.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            Some((stem.parse().ok()?, p.clone()))
        })
        .collect();
    found.sort();
    found.into_iter().map(|(_, p)| p).collect()
}
